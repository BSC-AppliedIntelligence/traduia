# Database initialisation image for a Supabase backend (see docs/database-init.md).
# Build from the repository root:  docker build -f db/Dockerfile -t traduia-db-init .
FROM alpine:3.20

LABEL org.opencontainers.image.title="traduia-db-init" \
      org.opencontainers.image.description="One-shot Supabase initialisation for TraduIA: role passwords, SQL migrations and bootstrap administrators" \
      org.opencontainers.image.source="https://github.com/BSC-AppliedIntelligence/traduia" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN apk add --no-cache bash postgresql16-client
COPY db/migrations /db/migrations
COPY db/init /db/init
USER 65534:65534
ENTRYPOINT ["bash", "/db/init/db-init.sh"]
