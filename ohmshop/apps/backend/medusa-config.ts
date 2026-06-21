import { loadEnv, defineConfig } from '@medusajs/framework/utils'

loadEnv(process.env.NODE_ENV || 'development', process.cwd())

module.exports = defineConfig({
  projectConfig: {
    databaseUrl: process.env.DATABASE_URL,
    redisUrl: process.env.REDIS_URL,
    // Limit each module's Knex pool to 1 connection to prevent pool deadlock
    // during db:migrate. Medusa v2 instantiates ~25 module pools in parallel;
    // with the default pool min=2 this exhausts connections before any are freed.
    databaseDriverOptions: {
      pool: {
        min: 1,
        max: 2,
        acquireTimeoutMillis: 60000,
        idleTimeoutMillis: 30000,
      },
    },
    http: {
      storeCors: process.env.STORE_CORS!,
      adminCors: process.env.ADMIN_CORS!,
      authCors: process.env.AUTH_CORS!,
      jwtSecret: process.env.JWT_SECRET,
      cookieSecret: process.env.COOKIE_SECRET,
    }
  }
})
