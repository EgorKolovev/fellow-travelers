FROM node:24.22.2-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_BOT_USERNAME=
ENV VITE_BOT_USERNAME=$VITE_BOT_USERNAME
RUN npm run build

FROM nginx:1.27.5-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
