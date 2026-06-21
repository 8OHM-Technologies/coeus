import json
import logging
import os
import urllib.parse
import requests

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Syncs scraper results to MedusaJS v2 ohmshop with automated category and product provisioning."

    def add_arguments(self, parser):
        parser.add_argument(
            "--now",
            action="store_true",
            help="Run the synchronization task immediately and exit.",
        )

    def handle(self, *args, **options):
        self.sync_to_medusa()

    def sync_to_medusa(self):
        logger.info("Starting MedusaJS v2 infrastructure synchronization...")

        # 1. Configuration Validation
        api_url = os.environ.get("MEDUSA_API_URL", "http://ohmshop-server:9000")
        admin_email = os.environ.get("MEDUSA_ADMIN_EMAIL", "tiaanf@8ohm.co.za")
        admin_password = os.environ.get("MEDUSA_ADMIN_PASSWORD", "!DNsEA#5kU")

        if not all([api_url, admin_email, admin_password]):
            logger.error("Missing primary Medusa connection settings.")
            return

        # Authenticate and obtain JWT token
        token = self.get_auth_token(api_url, admin_email, admin_password)
        if not token:
            logger.error("Could not obtain auth token from Medusa Admin API.")
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # 2. Fetch Defaults (Sales Channel, Shipping Profile, Stock Location)
        sales_channels = self.get_sales_channels(api_url, headers)
        if not sales_channels:
            logger.error("No Sales Channels found in Medusa. Aborting.")
            return
        default_sales_channel_id = sales_channels[0]["id"]

        shipping_profiles = self.get_shipping_profiles(api_url, headers)
        if not shipping_profiles:
            logger.error("No Shipping Profiles found in Medusa. Aborting.")
            return
        default_shipping_profile_id = shipping_profiles[0]["id"]

        stock_locations = self.get_stock_locations(api_url, headers)
        default_stock_location_id = stock_locations[0]["id"] if stock_locations else None

        # 3. Bootstrap Categories
        logger.info("Syncing and healing category tree...")
        category_map = self.bootstrap_categories(api_url, headers)

        # 4. Load Combined Product Processing Stream
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

        # Fetch existing products
        existing_products = self.get_existing_medusa_products(api_url, headers)
        current_handles = set()

        # 5. Process and Map Input Stream
        for item in products_data:
            ref = item.get("stock_code")
            if not ref:
                continue

            ref = self.sanitize_code(ref)
            handle = ref.lower()
            current_handles.add(handle)
            part_number = self.sanitize_code(item.get("part_number") or ref)

            # Execute heuristic routing classification rules engine
            category_slug, _, specifications = self.classify_product(item)

            # Resolve mapped category IDs
            category_id = category_map.get(category_slug)
            if not category_id and category_map.get("bundles-essentials"):
                category_id = category_map.get("bundles-essentials")
            
            category_ids = [category_id] if category_id else []

            pricing_data = item.get("pricing", [])
            cost_price_val = 0.0
            if pricing_data:
                cost_price_val = self.clean_price(pricing_data[0].get("price"))

            # Metadata properties
            metadata = {}
            for spec_key, spec_val in specifications.items():
                metadata[spec_key] = str(spec_val)
            if item.get("manufacturer"):
                metadata["manufacturer"] = str(item.get("manufacturer"))
            if item.get("moq"):
                metadata["minimum_order_quantity"] = str(item.get("moq"))

            # Determine variations
            tier_entries = [
                p for p in pricing_data if p.get("qty") not in ["markup", "no_discount"]
            ]
            
            # Prepare variants payload
            variants_payloads = []
            if not tier_entries:
                sale_price = next(
                    (p.get("price") for p in pricing_data if p.get("qty") == "markup"),
                    None,
                )
                price_val = self.clean_price(sale_price) or cost_price_val
                variants_payloads.append({
                    "title": "Standard Option",
                    "sku": part_number,
                    "prices": [
                        {
                            "currency_code": "zar",
                            "amount": int(price_val * 100),
                        }
                    ],
                    "options": {"Quantity Tier": "Standard"},
                    "manage_inventory": False, # Medusa handles inventory separately, let's keep variants simple per user request
                    "allow_backorder": True,
                })
            else:
                for tier in tier_entries:
                    qty_label = tier.get("qty")
                    tier_price_val = self.clean_price(tier.get("price"))
                    qty_slug = qty_label.lower().replace(" to ", "-").replace(" ", "_")
                    variant_sku = f"{part_number}_{qty_slug}"

                    variants_payloads.append({
                        "title": f"Qty: {qty_label}",
                        "sku": variant_sku,
                        "prices": [
                            {
                                "currency_code": "zar",
                                "amount": int(tier_price_val * 100),
                            }
                        ],
                        "options": {"Quantity Tier": qty_label},
                        "manage_inventory": False,
                        "allow_backorder": True,
                    })

            # Assemble Medusa Product Payload
            # In Medusa v2: POST /admin/products expects specific structure
            product_payload = {
                "title": item.get("description", "No Description"),
                "handle": handle,
                "description": item.get("memo") or item.get("description", ""),
                "status": "published",
                "is_giftcard": False,
                "discountable": True,
                "sales_channels": [{"id": default_sales_channel_id}],
                "shipping_profile_id": default_shipping_profile_id,
                "metadata": metadata,
                "options": [
                    {"title": "Quantity Tier"}
                ],
                "variants": variants_payloads
            }
            if category_ids:
                # Based on V2: "category_ids": ["cat_..."] or "categories": [{"id": ...}]
                product_payload["categories"] = [{"id": cid} for cid in category_ids]

            # Upload images if present
            image_urls = []
            if item.get("image_url"):
                image_urls.append(item.get("image_url"))
            if item.get("image_url2"):
                image_urls.append(item.get("image_url2"))
            
            seen = set()
            image_urls = [x for x in image_urls if x and not (x in seen or seen.add(x))]
            
            uploaded_images = []
            for img_url in image_urls:
                uploaded_url = self.upload_image(api_url, headers, img_url)
                if uploaded_url:
                    uploaded_images.append({"url": uploaded_url})
            
            if uploaded_images:
                product_payload["images"] = uploaded_images

            # Create or Update Product
            if handle in existing_products:
                logger.info(f"Updating product {handle}...")
                product_id = existing_products[handle]["id"]
                # For update, we might need to handle variants differently. Let's try simple PUT.
                self.update_product(api_url, headers, product_id, product_payload)
            else:
                logger.info(f"Creating product {handle}...")
                self.create_product(api_url, headers, product_payload)

        # 6. Mark Missing Products as draft (out of stock equivalent)
        # to_mark_out = [handle for handle in existing_products if handle not in current_handles]
        # if to_mark_out:
        #     for handle in to_mark_out:
        #         self.update_product(api_url, headers, existing_products[handle]["id"], {"status": "draft"})

        logger.info("MedusaJS inventory synchronization complete.")

    def get_auth_token(self, api_url, email, password):
        token_url = f"{api_url}/auth/user/emailpass"
        try:
            logger.info(f"Authenticating with Medusa at {token_url}...")
            resp = requests.post(
                token_url,
                json={"email": email, "password": password},
            )
            resp.raise_for_status()
            return resp.json().get("token")
        except Exception as e:
            logger.error(f"Failed to authenticate with Medusa API: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return None

    def get_sales_channels(self, api_url, headers):
        try:
            resp = requests.get(f"{api_url}/admin/sales-channels", headers=headers)
            resp.raise_for_status()
            return resp.json().get("sales_channels", [])
        except Exception as e:
            logger.error(f"Failed to fetch sales channels: {e}")
            return []

    def get_shipping_profiles(self, api_url, headers):
        try:
            resp = requests.get(f"{api_url}/admin/shipping-profiles", headers=headers)
            resp.raise_for_status()
            return resp.json().get("shipping_profiles", [])
        except Exception as e:
            logger.error(f"Failed to fetch shipping profiles: {e}")
            return []

    def get_stock_locations(self, api_url, headers):
        try:
            resp = requests.get(f"{api_url}/admin/stock-locations", headers=headers)
            resp.raise_for_status()
            return resp.json().get("stock_locations", [])
        except Exception as e:
            logger.error(f"Failed to fetch stock locations: {e}")
            return []

    def bootstrap_categories(self, api_url, headers):
        current_map = {}
        try:
            resp = requests.get(f"{api_url}/admin/product-categories?limit=100", headers=headers)
            resp.raise_for_status()
            categories = resp.json().get("product_categories", [])
            for c in categories:
                current_map[c.get("handle")] = c.get("id")
        except Exception as e:
            logger.warning(f"Failed to fetch categories: {e}")

        tree_blueprint = [
            {"handle": "development-boards", "name": "Development Boards", "parent": None},
            {"handle": "microcontrollers", "name": "Microcontrollers", "parent": "development-boards"},
            {"handle": "shields-breakouts", "name": "Shields & Breakouts", "parent": "development-boards"},
            {"handle": "power-control-relays", "name": "Power Control & Relays", "parent": None},
            {"handle": "mechanical-relays", "name": "Mechanical Relays", "parent": "power-control-relays"},
            {"handle": "solid-state-drivers", "name": "Solid State & Drivers", "parent": "power-control-relays"},
            {"handle": "sensors-inputs", "name": "Sensors & Inputs", "parent": None},
            {"handle": "displays-hmi", "name": "Displays & HMI", "parent": None},
            {"handle": "kits-components", "name": "Kits & Components", "parent": None},
            {"handle": "bundles-essentials", "name": "Bundles & Essentials", "parent": "kits-components"},
        ]

        # First pass to create missing
        for node in tree_blueprint:
            handle = node["handle"]
            if handle not in current_map:
                logger.info(f"Healed missing category: '{handle}'")
                parent_id = current_map.get(node["parent"]) if node["parent"] else None
                payload = {
                    "handle": handle,
                    "name": node["name"],
                    "is_active": True,
                    "is_internal": False,
                }
                if parent_id:
                    payload["parent_category_id"] = parent_id
                    
                try:
                    resp = requests.post(f"{api_url}/admin/product-categories", json=payload, headers=headers)
                    resp.raise_for_status()
                    new_cat = resp.json().get("product_category", {})
                    current_map[handle] = new_cat.get("id")
                except Exception as e:
                    logger.error(f"Failed provisioning category {handle}: {e}")
                    if hasattr(e, 'response') and e.response is not None:
                        logger.error(f"Response: {e.response.text}")

        return current_map

    def get_existing_medusa_products(self, api_url, headers):
        products_map = {}
        offset = 0
        limit = 50
        while True:
            try:
                resp = requests.get(f"{api_url}/admin/products", params={"offset": offset, "limit": limit}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                products = data.get("products", [])
                if not products:
                    break
                for p in products:
                    products_map[p.get("handle")] = p
                
                count = data.get("count", 0)
                offset += limit
                if offset >= count:
                    break
            except Exception as e:
                logger.error(f"Failed to fetch existing products: {e}")
                break
        return products_map

    def create_product(self, api_url, headers, payload):
        try:
            resp = requests.post(f"{api_url}/admin/products", json=payload, headers=headers)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to create product {payload.get('handle')}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def update_product(self, api_url, headers, product_id, payload):
        try:
            # We must be careful to only update safe fields, or simply POST /admin/products/{id}
            # Medusa expects variants to be updated separately sometimes, but let's try pushing the full payload.
            resp = requests.post(f"{api_url}/admin/products/{product_id}", json=payload, headers=headers)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to update product {product_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False

    def upload_image(self, api_url, headers, image_url):
        try:
            img_resp = requests.get(image_url, timeout=10)
            img_resp.raise_for_status()
            img_data = img_resp.content

            filename = os.path.basename(urllib.parse.urlparse(image_url).path) or "image.jpg"

            upload_headers = {
                "Authorization": headers["Authorization"],
            }
            files = {
                "files": (filename, img_data, "image/jpeg")
            }
            resp = requests.post(
                f"{api_url}/admin/uploads",
                headers=upload_headers,
                files=files
            )
            resp.raise_for_status()
            upload_data = resp.json().get("files", [])
            if upload_data:
                return upload_data[0].get("url")
            return None
        except Exception as e:
            logger.warning(f"Failed to upload image from {image_url}: {e}")
            return None

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

        if any(term in combined for term in ["DISPLAY", "SCREEN", "TFT", "LCD", "KEYBOARD", "MATRIX BUTTON"]):
            category_slug = "displays-hmi"
            product_type_slug = "displays"
            return category_slug, product_type_slug, specs

        if "SENSOR" in combined or "DETECTION MODULE" in combined or "MQ135" in combined or "HX711" in combined or "OBSTACLE" in combined:
            category_slug = "sensors-inputs"
            product_type_slug = "sensors"

            if "TEMPERATURE" in combined: specs["sensor-type"] = "Temperature"
            elif any(x in combined for x in ["AIR QUALITY", "GAS", "MQ-135", "MQ135"]): specs["sensor-type"] = "Gas/Air Quality"
            elif "OBSTACLE" in combined or "APDS-9930" in combined: specs["sensor-type"] = "Optical/Obstacle"
            elif any(x in combined for x in ["PRESSURE", "FORCE", "HX711"]): specs["sensor-type"] = "Force/Pressure"

            if "ANALOGUE" in combined: specs["interface"] = "Analog"
            elif "APDS-9930" in combined: specs["interface"] = "I2C"
            else: specs["interface"] = "Digital"
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

            if "1-CH" in combined or "SINGLE" in combined: specs["channel-count"] = "1-CH"
            elif "2-CH" in combined or "DUAL" in combined or "2-CHANNEL" in combined: specs["channel-count"] = "2-CH"
            elif "4-CH" in combined or "FOUR" in combined or "4-CHANNEL" in combined: specs["channel-count"] = "4-CH"
            elif "8-CH" in combined or "OCTAL" in combined or "8-CHANNEL" in combined: specs["channel-count"] = "8-CH"

            if "12V" in combined: specs["coil-trigger-voltage"] = "12V"
            elif "5V" in combined: specs["coil-trigger-voltage"] = "5V"
            elif "MICRO:BIT" in combined or "3.3V" in combined: specs["coil-trigger-voltage"] = "3.3V"

            if "30A" in combined: specs["switching-capacity"] = "30A"
            elif "10A" in combined: specs["switching-capacity"] = "10A"
            elif "3A" in combined: specs["switching-capacity"] = "3A"
            elif "2A" in combined: specs["switching-capacity"] = "2A"
            return category_slug, product_type_slug, specs

        if any(term in combined for term in ["ARDUINO", "MICROCONTROLLER", "DEVELOPMENT BOARD", "COMPATIBLE CONTROLLER", "MEGA2560", "NANO", "UNO"]):
            product_type_slug = "microcontrollers"
            if "SHIELD" in combined or "BREAKOUT" in combined or "JUMPER" in combined or "CABLE" in combined:
                category_slug = "shields-breakouts"
            else:
                category_slug = "microcontrollers"
                if "MEGA" in combined: specs["form-factor"] = "Mega"; specs["main-chip"] = "ATmega2560"
                elif "NANO" in combined: specs["form-factor"] = "Nano"; specs["main-chip"] = "ATmega328P"
                elif "LEONARDO" in combined: specs["form-factor"] = "Leonardo"; specs["main-chip"] = "ATmega32U4"
                elif "PRO MICRO" in combined: specs["form-factor"] = "Micro"; specs["main-chip"] = "ATmega32U4"
                elif "PRO MINI" in combined: specs["form-factor"] = "Pro Mini"; specs["main-chip"] = "ATmega328P"
                elif "UNO" in combined: specs["form-factor"] = "Uno"; specs["main-chip"] = "ATmega328P"

                if "USB-C" in combined: specs["usb-interface"] = "Type-C"
                elif "MINI" in combined: specs["usb-interface"] = "None"
                elif "MICRO" in combined or "LEONARDO" in combined: specs["usb-interface"] = "Micro-USB"
                else: specs["usb-interface"] = "Type-B"
            return category_slug, product_type_slug, specs

        return category_slug, product_type_slug, specs

    def clean_price(self, p_str):
        if p_str is None or p_str == "": return 0.0
        try:
            p_str = str(p_str)
            cleaned = "".join(c for c in p_str if c.isdigit() or c == ".")
            return float(cleaned) if cleaned else 0.0
        except Exception: return 0.0

    def sanitize_code(self, code_str):
        if not code_str: return ""
        s = str(code_str).strip().replace(" ", "-").replace("/", "-").replace("\\", "-")
        return "".join(c for c in s if c.isalnum() or c == "-").upper()