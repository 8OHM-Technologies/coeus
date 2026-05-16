import json
import logging
import os
import traceback
import urllib.parse

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Syncs combined scraper results to Saleor via GraphQL."

    def add_arguments(self, parser):
        parser.add_argument(
            "--now",
            action="store_true",
            help="Run the synchronization task immediately and exit.",
        )

    def handle(self, *args, **options):
        self.sync_to_saleor()

    def sync_to_saleor(self):
        logger.info("Starting Saleor synchronization...")

        # 1. Configuration
        api_url = os.environ.get("SALEOR_API_URL")
        token = os.environ.get("SALEOR_TOKEN")
        channel_slug = os.environ.get("SALEOR_CHANNEL_SLUG")
        warehouse_id = os.environ.get("SALEOR_WAREHOUSE_ID")
        product_type_id = os.environ.get("SALEOR_PRODUCT_TYPE_ID")
        category_id = os.environ.get("SALEOR_CATEGORY_ID")

        if not all(
            [api_url, token, channel_slug, warehouse_id, product_type_id, category_id]
        ):
            logger.error("Missing Saleor configuration in environment variables.")
            return

        # Decode IDs
        warehouse_id = urllib.parse.unquote(warehouse_id).strip("?")
        product_type_id = urllib.parse.unquote(product_type_id).strip("?")
        category_id = urllib.parse.unquote(category_id).strip("?")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # 1b. Resolve Channel ID
        channel_id = self.get_channel_id(api_url, headers, channel_slug)
        if not channel_id:
            logger.error(f"Could not resolve channel ID for slug '{channel_slug}'")
            return

        # 1c. Resolve Attribute IDs and Types
        attr_map = self.get_attribute_info(
            api_url, headers, ["manufacturer", "minimum-order-quantity"]
        )

        # 1d. Verify Attribute Assignment to Product Type (CRITICAL DIAGNOSTICS)
        self.verify_product_type_attributes(
            api_url, headers, product_type_id, [a["id"] for a in attr_map.values()]
        )

        # 2. Load Combined Results
        file_path = "/app/data/combined/combined_results.json"
        if not os.path.exists(file_path):
            file_path = os.path.join(
                settings.BASE_DIR.parent, "data", "combined", "combined_results.json"
            )

        if not os.path.exists(file_path):
            logger.error(f"Combined results file not found at {file_path}")
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                products_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load combined results: {e}")
            return

        # 3. Fetch Existing Products from Saleor (to handle updates/out-of-stock)
        existing_refs = self.get_existing_saleor_products(api_url, headers)

        # 4. Prepare Batches
        to_create = []
        to_update = []
        current_refs = set()

        for item in products_data:
            ref = item.get("stock_code")
            if not ref:
                continue

            current_refs.add(ref)

            # Extract prices
            sale_price = None
            cost_price = None
            pricing_data = item.get("pricing", [])

            if pricing_data:
                # Baseline cost price is the first entry
                cost_price = pricing_data[0].get("price")

            # Sale price is the 'markup' entry
            for p in pricing_data:
                if p.get("qty") == "markup":
                    sale_price = p.get("price")
                    break

            def clean_price(p_str):
                if p_str is None or p_str == "":
                    return 0.0
                try:
                    # Robust cleaning: keep only digits and period
                    p_str = str(p_str)
                    cleaned = "".join(c for c in p_str if c.isdigit() or c == ".")
                    if not cleaned:
                        return 0.0
                    return float(cleaned)
                except Exception as e:
                    logger.warning(f"Failed to clean price '{p_str}': {e}")
                    return 0.0

            sale_price_val = clean_price(sale_price)
            cost_price_val = clean_price(cost_price)

            if cost_price_val == 0.0 and pricing_data:
                logger.debug(
                    f"Cost price for {ref} was 0.0. Raw cost_price: '{cost_price}'. Pricing Data: {pricing_data}"
                )
            # Map Input
            product_input = {
                "name": item.get("description", "No Description"),
                "description": json.dumps(
                    {
                        "blocks": [
                            {
                                "data": {
                                    "text": item.get("memo")
                                    or item.get("description", "")
                                },
                                "type": "paragraph",
                            }
                        ]
                    }
                ),
                "externalReference": ref,
                "category": category_id,
                "productType": product_type_id,
                "attributes": [],
                "channelListings": [
                    {
                        "channelId": channel_id,
                        "isPublished": True,
                        "visibleInListings": True,
                    }
                ],
                "variants": [
                    {
                        "sku": item.get("part_number") or ref,
                        "externalReference": ref,
                        "attributes": [],
                        "trackInventory": True,
                        "stocks": [{"warehouse": warehouse_id, "quantity": 100}],
                        "channelListings": [
                            {
                                "channelId": channel_id,
                                "price": sale_price_val,
                                "costPrice": cost_price_val,
                            }
                        ],
                    }
                ],
            }

            # Add Attributes dynamically based on their type
            for slug, info in attr_map.items():
                attr_id = info["id"]
                attr_type = info["type"]

                val = ""
                if slug == "manufacturer":
                    val = str(item.get("manufacturer") or "Unknown")
                elif slug == "minimum-order-quantity":
                    val = str(item.get("moq") or "1")

                attr_obj = {"id": attr_id}

                # Saleor 3.x+ requires specific fields based on inputType
                if attr_type == "PLAIN_TEXT":
                    attr_obj["plainText"] = val
                elif attr_type == "NUMERIC":
                    attr_obj["numeric"] = val
                else:
                    # Fallback to values for DROPDOWN etc.
                    attr_obj["values"] = [val]

                product_input["attributes"].append(attr_obj)

            if item.get("image_url2"):
                product_input["media"] = [{"mediaUrl": item.get("image_url2")}]

            if ref in existing_refs:
                to_update.append((existing_refs[ref], product_input))
            else:
                to_create.append(product_input)

        # 5. Execute Create Batch
        if to_create:
            logger.info(f"Creating {len(to_create)} new products...")
            self.bulk_create_products(api_url, headers, to_create)

        # 6. Execute Update Batch
        if to_update:
            logger.info(f"Updating {len(to_update)} existing products...")
            for saleor_id, p_input in to_update:
                self.update_product(
                    api_url, headers, saleor_id, p_input, channel_id, warehouse_id
                )

        # 7. Mark Out of Stock
        to_mark_out = [ref for ref in existing_refs if ref not in current_refs]
        if to_mark_out:
            logger.info(f"Marking {len(to_mark_out)} products as out of stock...")
            for ref in to_mark_out:
                self.mark_out_of_stock(api_url, headers, ref, warehouse_id)

        logger.info("Saleor synchronization complete.")

    def get_channel_id(self, url, headers, slug):
        query = """
        query GetChannel($slug: String!) {
          channel(slug: $slug) {
            id
          }
        }
        """
        try:
            resp = requests.post(
                url, json={"query": query, "variables": {"slug": slug}}, headers=headers
            )
            data = resp.json()
            return (data.get("data") or {}).get("channel", {}).get("id")
        except Exception as e:
            logger.error(f"Failed to fetch channel ID: {e}")
            return None

    def get_attribute_info(self, url, headers, slugs):
        query = """
        query GetAttributes($slugs: [String!]!) {
          attributes(first: 50, filter: { slugs: $slugs }) {
            edges {
              node {
                id
                slug
                inputType
              }
            }
          }
        }
        """
        attr_map = {}
        try:
            resp = requests.post(
                url,
                json={"query": query, "variables": {"slugs": slugs}},
                headers=headers,
            )
            data = resp.json()
            edges = (data.get("data") or {}).get("attributes", {}).get("edges", [])
            for edge in edges:
                node = edge["node"]
                attr_map[node["slug"]] = {"id": node["id"], "type": node["inputType"]}
        except Exception as e:
            logger.error(f"Failed to fetch attribute IDs: {e}")
        return attr_map

    def verify_product_type_attributes(self, url, headers, pt_id, attr_ids):
        query = """
        query GetPT($id: ID!) {
          productType(id: $id) {
            id
            name
            productAttributes {
              id
              slug
              inputType
              valueRequired
            }
            variantAttributes {
              id
              slug
              inputType
              valueRequired
            }
          }
        }
        """
        try:
            resp = requests.post(
                url, json={"query": query, "variables": {"id": pt_id}}, headers=headers
            )
            data = resp.json()
            pt = (data.get("data") or {}).get("productType")
            if not pt:
                logger.error(f"Product Type {pt_id} not found!")
                return

            logger.error(
                f"--- !!! DIAGNOSTICS: Product Type '{pt['name']}' ({pt['id']}) !!! ---"
            )
            p_attrs = pt.get("productAttributes", [])
            v_attrs = pt.get("variantAttributes", [])

            logger.error("Product Level Attributes:")
            for attr in p_attrs:
                status = "ASSIGNED" if attr["id"] in attr_ids else "MISSING"
                req = "REQUIRED" if attr["valueRequired"] else "OPTIONAL"
                logger.error(
                    f"  - {attr['slug']} ({attr['id']}): {attr['inputType']} | {req} | {status}"
                )
                if attr["valueRequired"] and attr["id"] not in attr_ids:
                    logger.error(
                        f"!!! CRITICAL: Required product attribute '{attr['slug']}' is NOT being sent!"
                    )

            logger.error("Variant Level Attributes:")
            for attr in v_attrs:
                req = "REQUIRED" if attr["valueRequired"] else "OPTIONAL"
                logger.error(
                    f"  - {attr['slug']} ({attr['id']}): {attr['inputType']} | {req}"
                )
                if attr["valueRequired"]:
                    logger.error(
                        f"!!! CRITICAL: Required variant attribute '{attr['slug']}' is missing in script logic!"
                    )

            logger.error(
                "-----------------------------------------------------------------"
            )
        except Exception as e:
            logger.error(f"Failed to verify product type attributes: {e}")

    def get_existing_saleor_products(self, url, headers):
        query = """
        query GetProducts($after: String) {
          products(first: 100, after: $after) {
            pageInfo {
              hasNextPage
              endCursor
            }
            edges {
              node {
                id
                externalReference
              }
            }
          }
        }
        """
        refs = {}
        after = None
        while True:
            vars = {"after": after}
            try:
                resp = requests.post(
                    url, json={"query": query, "variables": vars}, headers=headers
                )
                data = resp.json()
                products = (data.get("data") or {}).get("products")
                if not products:
                    break
                for edge in products["edges"]:
                    node = edge["node"]
                    if node["externalReference"]:
                        refs[node["externalReference"]] = node["id"]
                if not products["pageInfo"]["hasNextPage"]:
                    break
                after = products["pageInfo"]["endCursor"]
            except Exception as e:
                logger.error(f"Failed to fetch products: {e}")
                break
        return refs

    def bulk_create_products(self, url, headers, products):
        mutation = """
        mutation ProductBulkCreate($products: [ProductBulkCreateInput!]!) {
          productBulkCreate(products: $products) {
            count
            results {
              product { id externalReference }
              errors { message code }
            }
            errors { message }
          }
        }
        """
        batch_size = 20
        for i in range(0, len(products), batch_size):
            batch = products[i : i + batch_size]
            try:
                resp = requests.post(
                    url,
                    json={"query": mutation, "variables": {"products": batch}},
                    headers=headers,
                )
                data = resp.json()
                if "errors" in data:
                    logger.error(f"Bulk Create API Validation Error: {data['errors']}")
                    logger.error(f"Sample Input: {json.dumps(batch[0], indent=2)}")
                    continue

                result = (data.get("data") or {}).get("productBulkCreate")
                if not result:
                    continue

                logger.info(f"Batch Create: Created {result.get('count', 0)} products.")
                for idx, res in enumerate(result.get("results", [])):
                    errors = res.get("errors")
                    if errors:
                        ref_in = batch[idx].get("externalReference") or "Unknown"
                        logger.error(f"SYNC FAILURE Row {idx} ({ref_in}): {errors}")
                        if idx == 0:
                            logger.error(
                                f"FAILED PAYLOAD: {json.dumps(batch[idx], indent=2)}"
                            )
            except Exception as e:
                logger.error(f"Batch Create Request Failed: {e}")

    def update_product(
        self, url, headers, product_id, p_input, channel_id, warehouse_id
    ):
        # 1. Update product basic info
        mutation_prod = """
        mutation ProductUpdate($id: ID!, $input: ProductInput!) {
          productUpdate(id: $id, input: $input) {
            product { id }
            errors { field message }
          }
        }
        """
        prod_input = {
            "name": p_input["name"],
            "description": p_input["description"],
            "category": p_input["category"],
            "attributes": p_input["attributes"],
        }
        try:
            resp = requests.post(
                url,
                json={
                    "query": mutation_prod,
                    "variables": {"id": product_id, "input": prod_input},
                },
                headers=headers,
            )
            data = resp.json()
            errs = (data.get("data") or {}).get("productUpdate", {}).get("errors")
            if errs:
                logger.warning(
                    f"Errors updating product info for {p_input['externalReference']}: {errs}"
                )

            # 2. Update variant basic info
            variant_data = p_input["variants"][0]
            mutation_var = """
            mutation ProductVariantUpdate($externalReference: String!, $input: ProductVariantInput!) {
              productVariantUpdate(externalReference: $externalReference, input: $input) {
                productVariant { id }
                errors { field message }
              }
            }
            """
            var_input = {
                "sku": variant_data["sku"],
                "trackInventory": True,
                "attributes": [],
            }
            resp = requests.post(
                url,
                json={
                    "query": mutation_var,
                    "variables": {
                        "externalReference": p_input["externalReference"],
                        "input": var_input,
                    },
                },
                headers=headers,
            )
            data = resp.json()
            errs = (
                (data.get("data") or {}).get("productVariantUpdate", {}).get("errors")
            )
            if errs:
                logger.warning(
                    f"Errors updating variant info for {p_input['externalReference']}: {errs}"
                )

            # 3. Update Stocks and Pricing in Bulk (Supports externalReference)
            mutation_bulk = """
            mutation ProductVariantBulkUpdate($productId: ID!, $variants: [ProductVariantBulkUpdateInput!]!) {
              productVariantBulkUpdate(product: $productId, variants: $variants) {
                count
                results {
                  productVariant { id externalReference }
                  errors { message code }
                }
                errors { message }
              }
            }
            """
            bulk_input = [
                {
                    "externalReference": p_input["externalReference"],
                    "stocks": [{"warehouse": warehouse_id, "quantity": 100}],
                    "channelListings": {
                        "update": [
                            {
                                "channelListing": channel_id,
                                "price": variant_data["channelListings"][0]["price"],
                                "costPrice": variant_data["channelListings"][0][
                                    "costPrice"
                                ],
                            }
                        ]
                    },
                }
            ]
            resp = requests.post(
                url,
                json={
                    "query": mutation_bulk,
                    "variables": {"productId": product_id, "variants": bulk_input},
                },
                headers=headers,
            )
            data = resp.json()
            result = (data.get("data") or {}).get("productVariantBulkUpdate")
            if not result:
                logger.error(
                    f"Bulk update failed for {p_input['externalReference']}. Response: {data}"
                )
            else:
                for res in result.get("results", []):
                    if res.get("errors"):
                        logger.warning(
                            f"Errors in bulk variant update for {p_input['externalReference']}: {res['errors']}"
                        )

        except Exception as e:
            logger.error(
                f"Failed to update product {p_input['externalReference']}: {e}"
            )

    def mark_out_of_stock(self, url, headers, external_ref, warehouse_id):
        mutation = """
        mutation StockBulkUpdate($stocks: [StockBulkUpdateInput!]!) {
          stockBulkUpdate(stocks: $stocks) {
            count
            errors { message }
          }
        }
        """
        stock_input = [
            {
                "variantExternalReference": external_ref,
                "warehouseId": warehouse_id,
                "quantity": 0,
            }
        ]
        try:
            requests.post(
                url,
                json={"query": mutation, "variables": {"stocks": stock_input}},
                headers=headers,
            )
        except Exception as e:
            logger.error(f"Failed to mark {external_ref} out of stock: {e}")
