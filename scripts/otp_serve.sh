#!/usr/bin/env bash
set -euo pipefail

OTP_DIR="${PS1_OTP_DIR:-/home/factory-user/ps1-scratch/otp}"
JAVA="${PS1_JAVA:-/home/factory-user/ps1-scratch/jdk/bin/java}"
JAR="$OTP_DIR/otp-shaded-2.8.1.jar"
GRAPH="$OTP_DIR/graph"

for input in "$JAR" "$GRAPH/graph.obj"; do
  if [[ ! -s "$input" ]]; then
    printf 'Missing required input: %s (run scripts/otp_build.sh first)\n' "$input" >&2
    exit 1
  fi
done

exec "$JAVA" -Xmx2500m -XX:+UseG1GC -jar "$JAR" --load --serve --port 3102 "$GRAPH"
