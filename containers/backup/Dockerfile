# Keep the upstream runtime frozen; this derivative only repairs public runtime
# access for Kubernetes restricted Pods. Never copy configuration or secrets here.
FROM ghcr.io/borgmatic-collective/borgmatic:2.2.0@sha256:14b70ae1354f9c1fbd5b790c372b948bc25cb00d45d0e6803916bb83cff6fe3c

USER 0:0
RUN /bin/busybox chown -R 0:0 /usr \
    && /bin/busybox chmod -R a+rX,go-w,u-s,g-s /usr \
    && (/bin/busybox awk -F: '$3 == 999 { found=1 } END { exit !found }' /etc/group \
        || echo 'backup:x:999:' >> /etc/group) \
    && (/bin/busybox awk -F: '$3 == 999 { found=1 } END { exit !found }' /etc/passwd \
        || echo 'backup:x:999:999:Backup runtime:/tmp:/bin/sh' >> /etc/passwd) \
    && /bin/busybox chown 0:0 /etc/passwd /etc/group \
    && /bin/busybox chmod a+r,go-w /etc/passwd /etc/group

ENV HOME=/tmp \
    XDG_CACHE_HOME=/cache \
    BORG_CACHE_DIR=/cache/borg
USER 999:999
ENTRYPOINT ["borgmatic"]
