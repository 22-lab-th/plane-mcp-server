# syntax=docker/dockerfile:1.7
# Build the official source because the old public MinIO images are unavailable.
# This image is used only in disposable compatibility tests.
FROM golang:1.24-alpine AS build
RUN apk add --no-cache git
WORKDIR /src
RUN git init && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth=1 origin f19c534b9f457773dcd043d977433e1a71525c3b \
    && git checkout --detach FETCH_HEAD
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    CGO_ENABLED=0 go build -trimpath -o /minio .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /minio /usr/local/bin/minio
ENTRYPOINT ["minio"]
