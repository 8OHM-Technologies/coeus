import { MedusaContainer } from "@medusajs/framework";
import {
  ContainerRegistrationKeys,
  ModuleRegistrationName,
  Modules,
  ProductStatus,
} from "@medusajs/framework/utils";
import {
  createApiKeysWorkflow,
  createCollectionsWorkflow,
  createInventoryLevelsWorkflow,
  createProductCategoriesWorkflow,
  createProductsWorkflow,
  createRegionsWorkflow,
  createSalesChannelsWorkflow,
  createShippingOptionsWorkflow,
  createShippingProfilesWorkflow,
  createStockLocationsWorkflow,
  createStoresWorkflow,
  createTaxRegionsWorkflow,
  linkSalesChannelsToApiKeyWorkflow,
  linkSalesChannelsToStockLocationWorkflow,
} from "@medusajs/medusa/core-flows";

export default async function initial_data_seed({
  container,
}: {
  container: MedusaContainer;
}) {
  const logger = container.resolve(ContainerRegistrationKeys.LOGGER);
  const link = container.resolve(ContainerRegistrationKeys.LINK);
  const query = container.resolve(ContainerRegistrationKeys.QUERY);
  const fulfillmentModuleService = container.resolve(
    ModuleRegistrationName.FULFILLMENT
  );

  const countries = ["za", "gb", "us"];

  logger.info("Seeding store data...");
  const {
    result: [defaultSalesChannel],
  } = await createSalesChannelsWorkflow(container).run({
    input: {
      salesChannelsData: [
        {
          name: "Default Sales Channel",
          description: "Created by Medusa",
        },
      ],
    },
  });

  const {
    result: [publishableApiKey],
  } = await createApiKeysWorkflow(container).run({
    input: {
      api_keys: [
        {
          title: "Default Publishable API Key",
          type: "publishable",
          created_by: "",
        },
      ],
    },
  });

  await linkSalesChannelsToApiKeyWorkflow(container).run({
    input: {
      id: publishableApiKey.id,
      add: [defaultSalesChannel.id],
    },
  });

  const {
    result: [store],
  } = await createStoresWorkflow(container).run({
    input: {
      stores: [
        {
          name: "Infinity Ohm Technologies Store",
          supported_currencies: [
            {
              currency_code: "zar",
              is_default: true,
            },
            {
              currency_code: "usd",
              is_default: false,
            },
            {
              currency_code: "eur",
              is_default: false,
            },
          ],
          default_sales_channel_id: defaultSalesChannel.id,
        },
      ],
    },
  });

  logger.info("Seeding region data...");
  const { result: regionResult } = await createRegionsWorkflow(container).run({
    input: {
      regions: [
        {
          name: "South Africa",
          currency_code: "zar",
          countries: ["za"],
          payment_providers: ["pp_system_default"],
        },
        {
          name: "International",
          currency_code: "usd",
          countries: ["gb", "us"],
          payment_providers: ["pp_system_default"],
        },
      ],
    },
  });
  const region = regionResult[0];
  logger.info("Finished seeding regions.");

  logger.info("Seeding tax regions...");
  await createTaxRegionsWorkflow(container).run({
    input: countries.map((country_code) => ({
      country_code,
      provider_id: "tp_system",
    })),
  });
  logger.info("Finished seeding tax regions.");

  logger.info("Seeding stock location data...");
  const { result: stockLocationResult } = await createStockLocationsWorkflow(
    container
  ).run({
    input: {
      locations: [
        {
          name: "Main Warehouse",
          address: {
            city: "Johannesburg",
            country_code: "ZA",
            address_1: "Infinity Road",
          },
        },
      ],
    },
  });
  const stockLocation = stockLocationResult[0];

  await link.create({
    [Modules.STOCK_LOCATION]: {
      stock_location_id: stockLocation.id,
    },
    [Modules.FULFILLMENT]: {
      fulfillment_provider_id: "manual_manual",
    },
  });

  logger.info("Seeding fulfillment data...");
  const { data: shippingProfileResult } = await query.graph({
    entity: "shipping_profile",
    fields: ["id"],
  });
  const shippingProfile = shippingProfileResult[0];

  const fulfillmentSet = await fulfillmentModuleService.createFulfillmentSets({
    name: "Standard Delivery",
    type: "shipping",
    service_zones: [
      {
        name: "Standard Zones",
        geo_zones: [
          {
            country_code: "za",
            type: "country",
          },
          {
            country_code: "gb",
            type: "country",
          },
          {
            country_code: "us",
            type: "country",
          },
        ],
      },
    ],
  });

  await link.create({
    [Modules.STOCK_LOCATION]: {
      stock_location_id: stockLocation.id,
    },
    [Modules.FULFILLMENT]: {
      fulfillment_set_id: fulfillmentSet.id,
    },
  });

  await createShippingOptionsWorkflow(container).run({
    input: [
      {
        name: "Standard Courier",
        price_type: "flat",
        provider_id: "manual_manual",
        service_zone_id: fulfillmentSet.service_zones[0].id,
        shipping_profile_id: shippingProfile.id,
        type: {
          label: "Standard",
          description: "Deliver within 2-5 business days.",
          code: "standard",
        },
        prices: [
          {
            currency_code: "zar",
            amount: 99,
          },
          {
            currency_code: "usd",
            amount: 10,
          },
          {
            region_id: region.id,
            amount: 99,
          },
        ],
        rules: [
          {
            attribute: "enabled_in_store",
            value: "true",
            operator: "eq",
          },
          {
            attribute: "is_return",
            value: "false",
            operator: "eq",
          },
        ],
      },
    ],
  });
  logger.info("Finished seeding fulfillment data.");

  await linkSalesChannelsToStockLocationWorkflow(container).run({
    input: {
      id: stockLocation.id,
      add: [defaultSalesChannel.id],
    },
  });
  logger.info("Finished seeding stock location data.");

  logger.info("Seeding product data...");

  const { result: categoryResult } = await createProductCategoriesWorkflow(
    container
  ).run({
    input: {
      product_categories: [
        {
          name: "Controllers",
          is_active: true,
        },
        {
          name: "Bridges",
          is_active: true,
        },
        {
          name: "Sensors",
          is_active: true,
        },
      ],
    },
  });

  await createProductsWorkflow(container).run({
    input: {
      products: [
        {
          title: "OhmGate Smart Gate Controller",
          category_ids: [
            categoryResult.find((cat) => cat.name === "Controllers")!.id,
          ],
          description:
            "Upgrade your existing gate motor to be smart, secure, and completely offline. Supports local logging, home automation integration (Home Assistant/MQTT), and physical overrides.",
          handle: "ohmgate-controller",
          weight: 250,
          status: ProductStatus.PUBLISHED,
          shipping_profile_id: shippingProfile.id,
          options: [
            {
              title: "Power Source",
              values: ["12V DC", "24V AC", "Battery Backup"],
            },
          ],
          variants: [
            {
              title: "OhmGate / 12V DC",
              sku: "OHM-GATE-12V",
              options: {
                "Power Source": "12V DC",
              },
              prices: [
                {
                  amount: 1450,
                  currency_code: "zar",
                },
                {
                  amount: 79,
                  currency_code: "usd",
                },
              ],
            },
            {
              title: "OhmGate / 24V AC",
              sku: "OHM-GATE-24V",
              options: {
                "Power Source": "24V AC",
              },
              prices: [
                {
                  amount: 1550,
                  currency_code: "zar",
                },
                {
                  amount: 85,
                  currency_code: "usd",
                },
              ],
            },
          ],
          sales_channels: [
            {
              id: defaultSalesChannel.id,
            },
          ],
        },
        {
          title: "OhmVoice Offline Assistant Node",
          category_ids: [
            categoryResult.find((cat) => cat.name === "Controllers")!.id,
          ],
          description:
            "A fully offline voice assistant node running local wake word and speech-to-text. Protects your privacy while providing instant local smart home control. Built-in high-quality microphone array.",
          handle: "ohmvoice-assistant",
          weight: 350,
          status: ProductStatus.PUBLISHED,
          shipping_profile_id: shippingProfile.id,
          options: [
            {
              title: "Finish",
              values: ["Anodized Silver", "Matte Obsidian"],
            },
          ],
          variants: [
            {
              title: "OhmVoice / Silver",
              sku: "OHM-VOICE-SILVER",
              options: {
                Finish: "Anodized Silver",
              },
              prices: [
                {
                  amount: 2999,
                  currency_code: "zar",
                },
                {
                  amount: 169,
                  currency_code: "usd",
                },
              ],
            },
            {
              title: "OhmVoice / Obsidian",
              sku: "OHM-VOICE-OBSIDIAN",
              options: {
                Finish: "Matte Obsidian",
              },
              prices: [
                {
                  amount: 2999,
                  currency_code: "zar",
                },
                {
                  amount: 169,
                  currency_code: "usd",
                },
              ],
            },
          ],
          sales_channels: [
            {
              id: defaultSalesChannel.id,
            },
          ],
        },
        {
          title: "OhmLink Alarm Panel Bridge",
          category_ids: [
            categoryResult.find((cat) => cat.name === "Bridges")!.id,
          ],
          description:
            "Integrate your legacy DSC PowerSeries or Texecom Premier alarm panel with your local network. Decodes physical keybus signals into secure local MQTT topics without breaking existing keypads.",
          handle: "ohmlink-alarm-bridge",
          weight: 150,
          status: ProductStatus.PUBLISHED,
          shipping_profile_id: shippingProfile.id,
          options: [
            {
              title: "Protocol",
              values: ["MQTT Only", "Home Assistant Native"],
            },
          ],
          variants: [
            {
              title: "OhmLink Bridge / HA Native",
              sku: "OHM-LINK-HA",
              options: {
                Protocol: "Home Assistant Native",
              },
              prices: [
                {
                  amount: 1950,
                  currency_code: "zar",
                },
                {
                  amount: 109,
                  currency_code: "usd",
                },
              ],
            },
          ],
          sales_channels: [
            {
              id: defaultSalesChannel.id,
            },
          ],
        },
      ],
    },
  });
  logger.info("Finished seeding product data.");

  logger.info("Seeding inventory levels.");

  const { data: inventoryItems } = await query.graph({
    entity: "inventory_item",
    fields: ["id"],
  });

  await createInventoryLevelsWorkflow(container).run({
    input: {
      inventory_levels: inventoryItems.map((item) => ({
        location_id: stockLocation.id,
        stocked_quantity: 1000,
        inventory_item_id: item.id,
      })),
    },
  });

  logger.info("Finished seeding inventory levels data.");
}
