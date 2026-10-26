# MediaMTX with our config and the DVR hook baked in. Baking (instead of bind-mounting) means
# Portainer git stacks need no host paths, and an editor replacing the file can never leave the
# container pointing at a deleted copy. Change config -> rebuild image -> redeploy.
FROM bluenviron/mediamtx:1.21.2-ffmpeg
COPY mediamtx.yml /mediamtx.yml
COPY dvr.sh /dvr.sh
RUN chmod 755 /dvr.sh
