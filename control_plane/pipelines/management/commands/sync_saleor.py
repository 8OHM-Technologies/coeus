import json
import logging
import os
import urllib.parse
import requests

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Syncs scraper results to Sylius with automated, self-healing category and product attribute provisioning."

    def add_arguments(self, parser):
        parser.add_argument(
            "--now",
            action="store_true",
            help="Run the synchronization task immediately and exit.",
        )

    def handle(self, *args, **options):
        self.sync_to_sylius()

    def sync_to_sylius(self):
        logger.info("Starting Sylius infrastructure synchronization...")

        # 1. Configuration Validation
        api_url = os.environ.get("SYLIUS_API_URL", "http://store-nginx-1/api/v2/admin")
        admin_email = os.environ.get("SYLIUS_ADMIN_EMAIL", "tiaanweb@gmail.com")
        admin_password = os.environ.get("SYLIUS_ADMIN_PASSWORD", "!DNsEA#5kU")
        channel_code = os.environ.get("SYLIUS_CHANNEL_CODE", "8OHM-ZA-WEBSTORE")
        locale_code = os.environ.get("SYLIUS_LOCALE_CODE", "en_ZA")

        if not all([api_url, admin_email, admin_password, channel_code, locale_code]):
            logger.error(
                "Missing primary Sylius connection settings in environment variables."
            )
            return

        # Authenticate and obtain JWT token
        token = self.get_auth_token(api_url, admin_email, admin_password)
        if not token:
            logger.error("Could not obtain auth token from Sylius Admin API.")
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # 2. Self-Healing Bootstrapper (Taxons & Attributes)
        logger.info("Syncing and healing category (taxon) tree and product attribute definitions...")
        root_taxon_code = self.get_root_taxon_code(api_url, headers)
        logger.info(f"Resolved root taxon code: {root_taxon_code}")

        taxon_map = self.bootstrap_taxons(api_url, headers, root_taxon_code, locale_code)
        
        target_attributes = {
            "manufacturer": "Manufacturer",
            "minimum-order-quantity": "Minimum Order Quantity",
            "volume-tier": "Volume Tier",
            "form-factor": "Form Factor",
            "main-chip": "Main Chip",
            "usb-interface": "USB Interface",
            "relay-type": "Relay Type",
            "channel-count": "Channel Count",
            "coil-trigger-voltage": "Coil Trigger Voltage",
            "switching-capacity": "Switching Capacity",
            "sensor-type": "Sensor Type",
            "interface": "Interface",
        }
        attr_map = self.bootstrap_attributes(api_url, headers, target_attributes, locale_code)

        # 3. Load Combined Product Processing Stream
        file_path = "/app/data/combined/combined_results.json"
        if not os.path.exists(file_path):
            file_path = os.path.join(
                settings.BASE_DIR.parent, "data", "combined", "combined_results.json"
            )

        if not os.path.exists(file_path):
            logger.error(
                f"Combined results file missing at target destination path: {file_path}"
            )
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                products_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load product source file data stream: {e}")
            return

        # Pull existing mapping from database instance to segment creation vs modification targets
        existing_products = self.get_existing_sylius_products(api_url, headers)

        current_refs = set()

        # 4. Process and Map Input Stream
        for item in products_data:
            ref = item.get("stock_code")
            if not ref:
                continue

            ref = self.sanitize_code(ref)
            current_refs.add(ref)
            part_number = self.sanitize_code(item.get("part_number") or ref)

            # Execute heuristic routing classification rules engine
            category_slug, _, specifications = self.classify_product(item)

            # Resolve mapped system IDs or utilize graceful global fallbacks
            category_code = taxon_map.get(category_slug) or taxon_map.get("bundles-essentials") or root_taxon_code

            pricing_data = item.get("pricing", [])
            cost_price_val = 0.0
            if pricing_data:
                cost_price_val = self.clean_price(pricing_data[0].get("price"))

            # Assemble clean standard product structural parameters
            product_payload = {
                "code": ref,
                "enabled": True,
                "translations": {
                    locale_code: {
                        "name": item.get("description", "No Description"),
                        "slug": ref.lower(),
                        "description": item.get("memo") or item.get("description", ""),
                        "shortDescription": item.get("description", "No Description")[:250],
                    }
                },
                "mainTaxon": f"/api/v2/admin/taxons/{category_code}",
                "productTaxons": [
                    {"taxon": f"/api/v2/admin/taxons/{category_code}"}
                ],
                "channels": [
                    f"/api/v2/admin/channels/{channel_code}"
                ],
                "attributes": []
            }

            # Map dynamically extracted top-level system features to specifications matrix
            for spec_key, spec_val in specifications.items():
                if spec_key in attr_map:
                    product_payload["attributes"].append({
                        "attribute": f"/api/v2/admin/product-attributes/{spec_key}",
                        "value": str(spec_val),
                        "localeCode": locale_code
                    })

            if "manufacturer" in attr_map:
                product_payload["attributes"].append({
                    "attribute": f"/api/v2/admin/product-attributes/manufacturer",
                    "value": str(item.get("manufacturer") or "Keyestudio"),
                    "localeCode": locale_code
                })
            if "minimum-order-quantity" in attr_map:
                product_payload["attributes"].append({
                    "attribute": f"/api/v2/admin/product-attributes/minimum-order-quantity",
                    "value": str(item.get("moq") or "1"),
                    "localeCode": locale_code
                })

            # Create or update product
            product_success = False
            if ref in existing_products:
                logger.info(f"Updating product {ref}...")
                update_payload = product_payload.copy()
                update_payload.pop("translations", None)
                product_success = self.update_product(api_url, headers, ref, update_payload)
            else:
                logger.info(f"Creating product {ref}...")
                product_success = self.create_product(api_url, headers, product_payload)

            if not product_success:
                logger.warning(f"Failed to upsert product {ref}. Skipping variants and images.")
                continue

            # Process variant payloads
            tier_entries = [
                p for p in pricing_data if p.get("qty") not in ["markup", "no_discount"]
            ]
            variants_payloads = []

            if not tier_entries:
                # Standalone default single-variant structure setup
                sale_price = next(
                    (p.get("price") for p in pricing_data if p.get("qty") == "markup"),
                    None,
                )
                price_val = self.clean_price(sale_price) or cost_price_val
                variants_payloads.append({
                    "code": part_number,
                    "product": f"/api/v2/admin/products/{ref}",
                    "enabled": True,
                    "translations": {
                        locale_code: {
                            "name": "Standard Option"
                        }
                    },
                    "channelPricings": {
                        channel_code: {
                            "price": int(price_val * 100),
                            "originalPrice": int(cost_price_val * 100)
                        }
                    },
                    "onHand": 100,
                    "tracked": True
                })
            else:
                # Build out dedicated variant structures for distinct bulk wholesale price curves
                for tier in tier_entries:
                    qty_label = tier.get("qty")
                    tier_price_val = self.clean_price(tier.get("price"))
                    qty_slug = qty_label.lower().replace(" to ", "-").replace(" ", "_")
                    variant_code = f"{part_number}_{qty_slug}"

                    variants_payloads.append({
                        "code": variant_code,
                        "product": f"/api/v2/admin/products/{ref}",
                        "enabled": True,
                        "translations": {
                            locale_code: {
                                "name": f"Qty: {qty_label}"
                            }
                        },
                        "channelPricings": {
                            channel_code: {
                                "price": int(tier_price_val * 100),
                                "originalPrice": int(cost_price_val * 100)
                            }
                        },
                        "onHand": 100,
                        "tracked": True
                    })

            # Create or update variants
            existing_vars = existing_products.get(ref, {}).get("variants", {})
            current_variant_codes = set()
            for v_payload in variants_payloads:
                v_code = v_payload["code"]
                current_variant_codes.add(v_code)
                if v_code in existing_vars:
                    self.update_variant(api_url, headers, v_code, v_payload)
                else:
                    self.create_variant(api_url, headers, v_payload)

            # Cleanup legacy variants of this product
            for old_v_code in existing_vars:
                if old_v_code not in current_variant_codes:
                    self.mark_variant_out_of_stock(api_url, headers, old_v_code)

            # Handle image uploads: clear existing, then upload new ones
            self.clear_product_images(api_url, headers, ref)
            
            image_urls = []
            if item.get("image_url"):
                image_urls.append(item.get("image_url"))
            if item.get("image_url2"):
                image_urls.append(item.get("image_url2"))
            
            # De-duplicate while preserving order
            seen = set()
            image_urls = [x for x in image_urls if x and not (x in seen or seen.add(x))]
            
            for img_url in image_urls:
                self.upload_product_image(api_url, headers, ref, img_url)

        # 5. Clean up completely missing products from scraper feed
        to_mark_out = [ref for ref in existing_products if ref not in current_refs]
        if to_mark_out:
            logger.info(
                f"Marking {len(to_mark_out)} items missing from scraper data feed as out of stock..."
            )
            for ref in to_mark_out:
                for old_v_code in existing_products[ref].get("variants", {}):
                    self.mark_variant_out_of_stock(api_url, headers, old_v_code)

        logger.info("Sylius inventory synchronization completely complete.")

    def get_auth_token(self, api_url, email, password):
        token_url = f"{api_url}/administrators/token"
        try:
            logger.info(f"Authenticating with Sylius at {token_url}...")
            resp = requests.post(
                token_url,
                json={"email": email, "password": password},
                headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            token = resp.json().get("token")
            return token
        except Exception as e:
            logger.error(f"Failed to authenticate with Sylius Admin API: {e}")
            return None

    def get_root_taxon_code(self, api_url, headers):
        try:
            resp = requests.get(f"{api_url}/taxons?limit=10", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("hydra:member") if isinstance(data, dict) else data
            if items:
                # Look for a taxon that has no parent
                for item in items:
                    if not item.get("parent"):
                        return item.get("code")
                return items[0].get("code")
        except Exception as e:
            logger.warning(f"Failed to fetch root taxon list: {e}")
        return "category"

    def bootstrap_taxons(self, api_url, headers, root_taxon_code, locale_code):
        current_map = {}
        try:
            resp = requests.get(f"{api_url}/taxons?limit=100", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("hydra:member") if isinstance(data, dict) else data
            if items:
                current_map = {item["code"]: item["code"] for item in items}
        except Exception as e:
            logger.warning(f"Failed to fetch taxons mapping: {e}")

        tree_blueprint = [
            {"slug": "development-boards", "name": "Development Boards", "parent": None},
            {"slug": "microcontrollers", "name": "Microcontrollers", "parent": "development-boards"},
            {"slug": "shields-breakouts", "name": "Shields & Breakouts", "parent": "development-boards"},
            {"slug": "power-control-relays", "name": "Power Control & Relays", "parent": None},
            {"slug": "mechanical-relays", "name": "Mechanical Relays", "parent": "power-control-relays"},
            {"slug": "solid-state-drivers", "name": "Solid State & Drivers", "parent": "power-control-relays"},
            {"slug": "sensors-inputs", "name": "Sensors & Inputs", "parent": None},
            {"slug": "displays-hmi", "name": "Displays & HMI", "parent": None},
            {"slug": "kits-components", "name": "Kits & Components", "parent": None},
            {"slug": "bundles-essentials", "name": "Bundles & Essentials", "parent": "kits-components"},
        ]

        for node in tree_blueprint:
            slug = node["slug"]
            if slug not in current_map:
                logger.info(f"Healed missing taxon entry: '{slug}'")
                parent_code = node["parent"] if node["parent"] else root_taxon_code
                payload = {
                    "code": slug,
                    "translations": {
                        locale_code: {
                            "name": node["name"],
                            "slug": f"{parent_code}/{slug}" if parent_code else slug
                        }
                    },
                    "parent": f"/api/v2/admin/taxons/{parent_code}"
                }
                try:
                    resp = requests.post(f"{api_url}/taxons", json=payload, headers=headers)
                    resp.raise_for_status()
                    current_map[slug] = slug
                except Exception as e:
                    logger.error(f"Failed provisioning taxon {slug}: {e}")

        return current_map

    def bootstrap_attributes(self, api_url, headers, target_attributes, locale_code):
        current_map = {}
        try:
            resp = requests.get(f"{api_url}/product-attributes?limit=100", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("hydra:member") if isinstance(data, dict) else data
            if items:
                current_map = {item["code"]: item["code"] for item in items}
        except Exception as e:
            logger.warning(f"Failed to fetch product attributes list: {e}")

        for code, name in target_attributes.items():
            if code not in current_map:
                logger.info(f"Healed missing product attribute definition: '{code}'")
                payload = {
                    "code": code,
                    "type": "text",
                    "storageType": "text",
                    "translations": {
                        locale_code: {
                            "name": name
                        }
                    }
                }
                try:
                    resp = requests.post(f"{api_url}/product-attributes", json=payload, headers=headers)
                    resp.raise_for_status()
                    current_map[code] = code
                except Exception as e:
                    logger.error(f"Failed provisioning attribute {code}: {e}")

        return current_map

    def get_existing_sylius_products(self, api_url, headers):
        products_map = {}
        page = 1
        while True:
            try:
                resp = requests.get(
                    f"{api_url}/products",
                    params={"page": page, "itemsPerPage": 100},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("hydra:member") if isinstance(data, dict) else data
                if not items:
                    break
                for item in items:
                    code = item.get("code")
                    if code:
                        variants = item.get("variants", [])
                        variants_dict = {}
                        for v in variants:
                            if isinstance(v, dict):
                                v_code = v.get("code")
                                if v_code:
                                    variants_dict[v_code] = v
                            elif isinstance(v, str):
                                v_code = v.split("/")[-1]
                                variants_dict[v_code] = {"code": v_code}
                        products_map[code] = {
                            "code": code,
                            "variants": variants_dict,
                        }
                if isinstance(data, list) or "hydra:view" not in data or "hydra:next" not in data.get("hydra:view", {}):
                    break
                page += 1
            except Exception as e:
                logger.error(f"Failed to fetch existing Sylius products: {e}")
                break
        return products_map

    def create_product(self, api_url, headers, payload):
        try:
            resp = requests.post(f"{api_url}/products", json=payload, headers=headers)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to create product {payload['code']}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def update_product(self, api_url, headers, code, payload):
        try:
            resp = requests.put(f"{api_url}/products/{code}", json=payload, headers=headers)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to update product {code}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def create_variant(self, api_url, headers, payload):
        try:
            resp = requests.post(f"{api_url}/product-variants", json=payload, headers=headers)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to create variant {payload['code']}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def get_sylius_db_connection(self):
        import psycopg2
        try:
            return psycopg2.connect(
                dbname=os.environ.get("SYLIUS_DATABASE_NAME", "sylius"),
                user=os.environ.get("POSTGRES_USER", "coeus-admin"),
                password=os.environ.get("POSTGRES_PASSWORD", "VMrc|r6hcGM<zeUN"),
                host=os.environ.get("POSTGRES_HOST", "cloud-sql-proxy"),
                port=os.environ.get("POSTGRES_PORT", "5432")
            )
        except Exception as e:
            logger.error(f"Failed to connect to Sylius database: {e}")
            return None

    def update_variant(self, api_url, headers, code, payload):
        conn = self.get_sylius_db_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cur:
                # 1. Update variant quantity (on_hand)
                quantity = payload.get("onHand", 100)
                cur.execute(
                    "UPDATE sylius_product_variant SET on_hand = %s WHERE code = %s RETURNING id",
                    (quantity, code)
                )
                row = cur.fetchone()
                if not row:
                    logger.warning(f"Variant with code {code} not found in database for SQL update.")
                    return False
                variant_id = row[0]

                # 2. Update channel pricings
                channel_pricings = payload.get("channelPricings", {})
                for ch_code, price_data in channel_pricings.items():
                    price = price_data.get("price", 0)
                    original_price = price_data.get("originalPrice", price)

                    # Check if channel pricing exists
                    cur.execute(
                        "SELECT id FROM sylius_channel_pricing WHERE product_variant_id = %s AND channel_code = %s",
                        (variant_id, ch_code)
                    )
                    cp_row = cur.fetchone()
                    if cp_row:
                        cur.execute(
                            "UPDATE sylius_channel_pricing SET price = %s, original_price = %s WHERE id = %s",
                            (price, original_price, cp_row[0])
                        )
                    else:
                        cur.execute(
                            "INSERT INTO sylius_channel_pricing (product_variant_id, channel_code, price, original_price) VALUES (%s, %s, %s, %s)",
                            (variant_id, ch_code, price, original_price)
                        )
                conn.commit()
                logger.info(f"Successfully updated variant {code} and its pricing via SQL.")
                return True
        except Exception as e:
            logger.error(f"Failed to update variant {code} via SQL: {e}")
            return False
        finally:
            conn.close()

    def mark_variant_out_of_stock(self, api_url, headers, code):
        conn = self.get_sylius_db_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sylius_product_variant SET on_hand = 0 WHERE code = %s",
                    (code,)
                )
                conn.commit()
                logger.info(f"Marked variant {code} out of stock via SQL.")
        except Exception as e:
            logger.error(f"Failed to mark variant {code} out of stock via SQL: {e}")
        finally:
            conn.close()

    def clear_product_images(self, api_url, headers, product_code):
        try:
            resp = requests.get(f"{api_url}/products/{product_code}/images", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("hydra:member") if isinstance(data, dict) else data
                if items:
                    logger.info(f"Clearing {len(items)} existing images for product {product_code} via API...")
                    for img in items:
                        img_id = img.get("id")
                        if img_id:
                            del_resp = requests.delete(
                                f"{api_url}/products/{product_code}/images/{img_id}",
                                headers=headers
                            )
                            if del_resp.status_code not in [200, 204]:
                                logger.warning(
                                    f"Failed to delete image {img_id} for product {product_code} via API: {del_resp.status_code}"
                                )
                return
        except Exception as e:
            logger.warning(f"Error listing/deleting images via API for product {product_code}: {e}")

        # Fallback: direct SQL deletion
        logger.info(f"Falling back to direct SQL deletion of images for product {product_code}...")
        conn = self.get_sylius_db_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM sylius_product_image_product_variants
                    WHERE image_id IN (
                        SELECT id FROM sylius_product_image 
                        WHERE owner_id = (SELECT id FROM sylius_product WHERE code = %s)
                    )
                """, (product_code,))
                cur.execute(
                    "DELETE FROM sylius_product_image WHERE owner_id = (SELECT id FROM sylius_product WHERE code = %s)",
                    (product_code,)
                )
                conn.commit()
                logger.info(f"Successfully cleared images for product {product_code} via SQL.")
        except Exception as e:
            logger.error(f"Failed to clear images for product {product_code} via SQL: {e}")
        finally:
            conn.close()

    def upload_product_image(self, api_url, headers, product_code, image_url):
        try:
            img_resp = requests.get(image_url, timeout=10)
            img_resp.raise_for_status()
            img_data = img_resp.content

            filename = os.path.basename(urllib.parse.urlparse(image_url).path) or "image.jpg"

            upload_headers = {
                "Authorization": headers["Authorization"],
                "Accept": "application/json"
            }
            files = {
                "file": (filename, img_data, "image/jpeg")
            }
            data = {
                "type": "main"
            }
            resp = requests.post(
                f"{api_url}/products/{product_code}/images",
                headers=upload_headers,
                files=files,
                data=data
            )
            resp.raise_for_status()
            logger.info(f"Uploaded image for product {product_code}")
        except Exception as e:
            logger.warning(f"Failed to upload image from {image_url} for product {product_code}: {e}")

    def product_has_image(self, product_code):
        conn = self.get_sylius_db_connection()
        if not conn:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM sylius_product_image WHERE owner_id = (SELECT id FROM sylius_product WHERE code = %s)",
                    (product_code,)
                )
                row = cur.fetchone()
                return row[0] > 0 if row else False
        except Exception as e:
            logger.error(f"Failed to check product image existence for {product_code}: {e}")
            return False
        finally:
            conn.close()

    def classify_product(self, item):
        desc = (item.get("description") or "").upper()
        memo = (item.get("memo") or "").upper()
        part = (item.get("part_number") or "").upper()
        combined = f"{desc} {memo} {part}"

        category_slug = "bundles-essentials"
        product_type_slug = "general-components"
        specs = {}

        if "KIT" in combined or "ASSORTMENT" in combined:
            category_slug = "bundles-essentials"
            product_type_slug = "kits"
            return category_slug, product_type_slug, specs

        if any(
            term in combined
            for term in ["DISPLAY", "SCREEN", "TFT", "LCD", "KEYBOARD", "MATRIX BUTTON"]
        ):
            category_slug = "displays-hmi"
            product_type_slug = "displays"
            return category_slug, product_type_slug, specs

        if (
            "SENSOR" in combined
            or "DETECTION MODULE" in combined
            or "MQ135" in combined
            or "HX711" in combined
            or "OBSTACLE" in combined
        ):
            category_slug = "sensors-inputs"
            product_type_slug = "sensors"

            if "TEMPERATURE" in combined:
                specs["sensor-type"] = "Temperature"
            elif any(x in combined for x in ["AIR QUALITY", "GAS", "MQ-135", "MQ135"]):
                specs["sensor-type"] = "Gas/Air Quality"
            elif "OBSTACLE" in combined or "APDS-9930" in combined:
                specs["sensor-type"] = "Optical/Obstacle"
            elif any(x in combined for x in ["PRESSURE", "FORCE", "HX711"]):
                specs["sensor-type"] = "Force/Pressure"

            if "ANALOGUE" in combined:
                specs["interface"] = "Analog"
            elif "APDS-9930" in combined:
                specs["interface"] = "I2C"
            else:
                specs["interface"] = "Digital"
            return category_slug, product_type_slug, specs

        if "RELAY" in combined or "MOSFET" in combined or "DRIVER MODULE" in combined:
            product_type_slug = "power-relays"
            if "SOLID STATE" in combined or "SSR" in combined:
                category_slug = "solid-state-drivers"
                specs["relay-type"] = "Solid State"
            elif "MOSFET" in combined or "IRF540" in combined:
                category_slug = "solid-state-drivers"
                specs["relay-type"] = "MOSFET"
            else:
                category_slug = "mechanical-relays"
                specs["relay-type"] = "Mechanical"

            if "1-CH" in combined or "SINGLE" in combined:
                specs["channel-count"] = "1-CH"
            elif "2-CH" in combined or "DUAL" in combined or "2-CHANNEL" in combined:
                specs["channel-count"] = "2-CH"
            elif "4-CH" in combined or "FOUR" in combined or "4-CHANNEL" in combined:
                specs["channel-count"] = "4-CH"
            elif "8-CH" in combined or "OCTAL" in combined or "8-CHANNEL" in combined:
                specs["channel-count"] = "8-CH"

            if "12V" in combined:
                specs["coil-trigger-voltage"] = "12V"
            elif "5V" in combined:
                specs["coil-trigger-voltage"] = "5V"
            elif "MICRO:BIT" in combined or "3.3V" in combined:
                specs["coil-trigger-voltage"] = "3.3V"

            if "30A" in combined:
                specs["switching-capacity"] = "30A"
            elif "10A" in combined:
                specs["switching-capacity"] = "10A"
            elif "3A" in combined:
                specs["switching-capacity"] = "3A"
            elif "2A" in combined:
                specs["switching-capacity"] = "2A"
            return category_slug, product_type_slug, specs

        if any(
            term in combined
            for term in [
                "ARDUINO",
                "MICROCONTROLLER",
                "DEVELOPMENT BOARD",
                "COMPATIBLE CONTROLLER",
                "MEGA2560",
                "NANO",
                "UNO",
            ]
        ):
            product_type_slug = "microcontrollers"
            if (
                "SHIELD" in combined
                or "BREAKOUT" in combined
                or "JUMPER" in combined
                or "CABLE" in combined
            ):
                category_slug = "shields-breakouts"
            else:
                category_slug = "microcontrollers"
                if "MEGA" in combined:
                    specs["form-factor"] = "Mega"
                    specs["main-chip"] = "ATmega2560"
                elif "NANO" in combined:
                    specs["form-factor"] = "Nano"
                    specs["main-chip"] = "ATmega328P"
                elif "LEONARDO" in combined:
                    specs["form-factor"] = "Leonardo"
                    specs["main-chip"] = "ATmega32U4"
                elif "PRO MICRO" in combined:
                    specs["form-factor"] = "Micro"
                    specs["main-chip"] = "ATmega32U4"
                elif "PRO MINI" in combined:
                    specs["form-factor"] = "Pro Mini"
                    specs["main-chip"] = "ATmega328P"
                elif "UNO" in combined:
                    specs["form-factor"] = "Uno"
                    specs["main-chip"] = "ATmega328P"

                if "USB-C" in combined:
                    specs["usb-interface"] = "Type-C"
                elif "MINI" in combined:
                    specs["usb-interface"] = "None"
                elif "MICRO" in combined or "LEONARDO" in combined:
                    specs["usb-interface"] = "Micro-USB"
                else:
                    specs["usb-interface"] = "Type-B"
            return category_slug, product_type_slug, specs

        return category_slug, product_type_slug, specs

    def clean_price(self, p_str):
        if p_str is None or p_str == "":
            return 0.0
        try:
            p_str = str(p_str)
            cleaned = "".join(c for c in p_str if c.isdigit() or c == ".")
            return float(cleaned) if cleaned else 0.0
        except Exception:
            return 0.0

    def sanitize_code(self, code_str):
        if not code_str:
            return ""
        import re
        # Convert spaces/parentheses/slashes to dashes/underscores
        cleaned = code_str.replace(" ", "_").replace("(", "_").replace(")", "_")
        # Remove any other character that is not alphanumeric, dash, or underscore
        cleaned = re.sub(r'[^a-zA-Z0-9\-_]', '', cleaned)
        # Simplify multiples
        cleaned = re.sub(r'_{2,}', '_', cleaned)
        cleaned = re.sub(r'-{2,}', '-', cleaned)
        return cleaned
