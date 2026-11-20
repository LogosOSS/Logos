#!/bin/sh
# Install the exact Temurin security release after verifying its archive and JVM.
set -eu

image_type="${1:?expected jdk or jre}"
install_dir=/opt/java/openjdk
case "$(uname -m)" in
    x86_64) architecture=x64 ;;
    aarch64|arm64) architecture=aarch64 ;;
    *) echo "Unsupported Java architecture" >&2; exit 1 ;;
esac
case "$image_type:$architecture" in
    jdk:x64) checksum=760f91b22c3efc9bd1b64ff950598231be82928cc807e30691a0e7a2995a62c0 ;;
    jdk:aarch64) checksum=c37879e0d08eeb1baa69fcbb7758cd455b7b1c0ceab1e2a136ccb1713c530984 ;;
    jre:x64) checksum=2edef9e4f81ec5ee426d023336d4cfbd7b4644e99884e7a73a6a867b70b17acf ;;
    jre:aarch64) checksum=c6161dcfb365024fbfbcda265343d562d6f821c78dd8e7aeaeaf294893f6ff8b ;;
    *) echo "Expected jdk or jre" >&2; exit 1 ;;
esac
archive="OpenJDK25U-${image_type}_${architecture}_linux_hotspot_25.0.5.1_2.tar.gz"
url="https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.5.1%2B2/${archive}"
scratch_dir="$(mktemp -d)"
trap 'rm -rf "$scratch_dir"' EXIT HUP INT TERM
curl --proto =https --tlsv1.2 -fsSL "$url" -o "$scratch_dir/java.tar.gz"
printf '%s  %s\n' "$checksum" "$scratch_dir/java.tar.gz" | sha256sum -c -
mkdir "$scratch_dir/runtime"
tar -xzf "$scratch_dir/java.tar.gz" --strip-components=1 -C "$scratch_dir/runtime"
version="$("$scratch_dir/runtime/bin/java" -version 2>&1)"
printf '%s\n' "$version" | grep -F 'build 25.0.5.1+2'
# Leave the existing runtime intact if download, checksum or version checks fail.
rm -rf "$install_dir"
mkdir -p "$install_dir"
cp -a "$scratch_dir/runtime/." "$install_dir/"
