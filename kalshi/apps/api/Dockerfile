FROM node:20-alpine AS base
WORKDIR /app

# Install shared package
COPY packages/shared/package.json packages/shared/
COPY packages/shared/tsconfig.json packages/shared/
COPY packages/shared/src packages/shared/src/

# Install api
COPY apps/api/package.json apps/api/
COPY package.json turbo.json tsconfig.base.json ./

RUN npm install --workspace=packages/shared --workspace=apps/api

# Build shared first, then api
COPY packages/shared packages/shared
RUN npm run build --workspace=packages/shared

COPY apps/api apps/api
RUN npm run build --workspace=apps/api

# Production image
FROM node:20-alpine AS runner
WORKDIR /app

COPY --from=base /app/node_modules ./node_modules
COPY --from=base /app/packages/shared/dist ./packages/shared/dist
COPY --from=base /app/packages/shared/package.json ./packages/shared/
COPY --from=base /app/apps/api/dist ./apps/api/dist
COPY --from=base /app/apps/api/package.json ./apps/api/
COPY --from=base /app/apps/api/src/db/migrations ./apps/api/src/db/migrations

ENV NODE_ENV=production
ENV NODE_OPTIONS="--max-old-space-size=512"
EXPOSE 3100

CMD ["node", "apps/api/dist/index.js"]
