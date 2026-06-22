FROM node:20-alpine AS base

# 1. Install system utilities first
RUN apk add --no-cache curl

# 2. Install pnpm globally
RUN npm install -g pnpm@10.11.1

WORKDIR /app

# Copy lockfile and workspace files
COPY pnpm-lock.yaml pnpm-workspace.yaml package.json turbo.json ./
COPY apps/backend/package.json ./apps/backend/
COPY apps/storefront/package.json ./apps/storefront/

# Install all dependencies (dev + prod)
RUN pnpm install --frozen-lockfile

# Copy the rest of the files
COPY . .

# --- BACKEND BUILDER ---
FROM base AS backend-builder
RUN pnpm --filter=@dtc/backend build

# --- STOREFRONT BUILDER ---
FROM base AS storefront-builder
ARG NEXT_PUBLIC_MEDUSA_BACKEND_URL
ARG NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY
ARG NEXT_PUBLIC_DEFAULT_REGION=za
ARG NEXT_PUBLIC_BASE_URL
ENV NEXT_PUBLIC_MEDUSA_BACKEND_URL=${NEXT_PUBLIC_MEDUSA_BACKEND_URL}
ENV NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY=${NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY}
ENV NEXT_PUBLIC_DEFAULT_REGION=${NEXT_PUBLIC_DEFAULT_REGION}
ENV NEXT_PUBLIC_BASE_URL=${NEXT_PUBLIC_BASE_URL}
RUN pnpm --filter=@dtc/storefront build

# --- BACKEND RUNTIME (PRODUCTION) ---
FROM node:20-alpine AS backend
RUN apk add --no-cache curl
RUN npm install -g pnpm@10.11.1
WORKDIR /app
COPY --from=backend-builder /app /app
ENV NODE_ENV=production
EXPOSE 9000
CMD ["pnpm", "--filter", "@dtc/backend", "start"]

# --- STOREFRONT RUNTIME (PRODUCTION) ---
FROM node:20-alpine AS storefront
RUN apk add --no-cache curl
RUN npm install -g pnpm@10.11.1
WORKDIR /app
COPY --from=storefront-builder /app /app
ENV NODE_ENV=production
EXPOSE 8000
CMD ["pnpm", "--filter", "@dtc/storefront", "start"]
