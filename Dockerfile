FROM cm-base:latest

USER root

# Copy entrypoint script
COPY --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 22

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=5 \
    CMD nc -z 127.0.0.1 22 || exit 1

ENTRYPOINT ["/entrypoint.sh"]
