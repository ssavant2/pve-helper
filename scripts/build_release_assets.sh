#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=${1:-"$repo_root/dist"}

mkdir -p "$output_dir"
cp "$repo_root/docker-compose.production.yml" "$output_dir/docker-compose.yml"
cp "$repo_root/.env.example" "$output_dir/example.env"

printf 'Created %s and %s\n' \
  "$output_dir/docker-compose.yml" \
  "$output_dir/example.env"
