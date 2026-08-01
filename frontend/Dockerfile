# Image base pinned à un tag semver patch.
# Pour update : vérifier les nouvelles patches sur hub.docker.com, mettre à jour le tag, rebuilder, tester.
# Cible de fréquence : trimestrielle.

FROM node:26.5.0-alpine3.23 AS build
WORKDIR /app
# package-lock.json requis — npm ci echoue explicitement si absent ou desynchronise
COPY package.json package-lock.json ./
RUN npm ci --legacy-peer-deps
COPY . .
RUN npm run build

FROM nginx:1.29.8-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# Copie comme template — ${API_URL} sera remplace au demarrage par envsubst
COPY nginx.conf /etc/nginx/conf.d/default.conf.template

EXPOSE 3000

# Valeur par defaut — a surcharger en prod via variable d'env API_URL
ENV API_URL=http://localhost:8000

# envsubst '${API_URL}' substitue uniquement ${API_URL}, laisse intact les variables nginx ($uri, $args...)
CMD ["/bin/sh", "-c", "envsubst '${API_URL}' < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"]
