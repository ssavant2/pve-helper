#!/usr/bin/env bash
set -euo pipefail

readonly CODEQL_VERSION="2.25.5"
readonly CODEQL_SHA256="24717f939f1bef659f893ff4a9c99ba8c056fbaca9640f877c4dc74cf96486d7"
readonly CODEQL_URL="https://github.com/github/codeql-action/releases/download/codeql-bundle-v${CODEQL_VERSION}/codeql-bundle-linux64.tar.gz"
readonly ANALYSIS_IMAGE="mcr.microsoft.com/playwright:v1.61.1-noble@sha256:5b8f294aff9041b7191c34a4bab3ac270157a28774d4b0660e9743297b697e48"

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CACHE_ROOT="${CODEQL_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/pve-helper-codeql}"
readonly ARCHIVE_PATH="${CACHE_ROOT}/codeql-bundle-${CODEQL_VERSION}.tar.gz"
readonly INSTALL_PATH="${CACHE_ROOT}/codeql-${CODEQL_VERSION}"
readonly INSTALL_MARKER="${INSTALL_PATH}/.pve-helper-bundle-sha256"
readonly THREADS="${CODEQL_THREADS:-2}"
readonly RAM_MB="${CODEQL_RAM_MB:-3072}"
readonly MODEL_PACK="ssavant2/pve-helper-storage-python"
readonly MODEL_PACK_ROOT="/workspace/.github/codeql/extensions"

for command in curl docker flock sha256sum tar; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Required command is missing: ${command}" >&2
        exit 2
    fi
done

mkdir -p "${CACHE_ROOT}"
exec 9>"${CACHE_ROOT}/install.lock"
flock 9

if [[ ! -x "${INSTALL_PATH}/codeql" ]] || [[ ! -f "${INSTALL_MARKER}" ]] || \
   [[ "$(<"${INSTALL_MARKER}" 2>/dev/null || true)" != "${CODEQL_SHA256}" ]]; then
    if [[ ! -f "${ARCHIVE_PATH}" ]] || ! echo "${CODEQL_SHA256}  ${ARCHIVE_PATH}" | sha256sum --check --status; then
        rm -f "${ARCHIVE_PATH}"
        echo "Downloading official CodeQL bundle v${CODEQL_VERSION} (cached after the first run)..."
        curl --fail --location --retry 3 --output "${ARCHIVE_PATH}.part" "${CODEQL_URL}"
        mv "${ARCHIVE_PATH}.part" "${ARCHIVE_PATH}"
    fi
    echo "${CODEQL_SHA256}  ${ARCHIVE_PATH}" | sha256sum --check --status

    install_tmp="$(mktemp -d "${CACHE_ROOT}/codeql-install.XXXXXX")"
    trap 'rm -rf "${install_tmp:-}" "${run_dir:-}"' EXIT
    tar -xzf "${ARCHIVE_PATH}" -C "${install_tmp}" --strip-components=1
    printf '%s\n' "${CODEQL_SHA256}" >"${install_tmp}/.pve-helper-bundle-sha256"
    rm -rf "${INSTALL_PATH}"
    mv "${install_tmp}" "${INSTALL_PATH}"
    install_tmp=""
fi

flock -u 9
run_dir="$(mktemp -d "${CACHE_ROOT}/codeql-run.XXXXXX")"
trap 'rm -rf "${install_tmp:-}" "${run_dir:-}"' EXIT

run_language() {
    local language="$1"
    local suite="$2"
    local database="/codeql-cache/$(basename "${run_dir}")/${language}-db"
    local sarif="/codeql-cache/$(basename "${run_dir}")/${language}.sarif"
    local log_path="${run_dir}/${language}.log"

    echo "Running CodeQL ${CODEQL_VERSION}: ${language}"
    if docker run --rm \
        --user "$(id -u):$(id -g)" \
        --env HOME=/tmp \
        --volume "${CACHE_ROOT}:/codeql-cache" \
        --volume "${REPO_ROOT}:/workspace:ro" \
        --workdir /workspace \
        "${ANALYSIS_IMAGE}" \
        bash -euo pipefail -c '
            codeql="$1"
            database="$2"
            language="$3"
            suite="$4"
            sarif="$5"
            threads="$6"
            ram="$7"
            model_pack_root="$8"
            model_pack="$9"
            "${codeql}" database create "${database}" \
                --language="${language}" \
                --source-root=/workspace \
                --codescanning-config=/workspace/.github/codeql/codeql-config.yml \
                --threads="${threads}"
            "${codeql}" database analyze "${database}" "${suite}" \
                --additional-packs="${model_pack_root}" \
                --model-packs="${model_pack}" \
                --format=sarif-latest \
                --output="${sarif}" \
                --threads="${threads}" \
                --ram="${ram}"
            node - "${sarif}" "${language}" <<"NODE"
const fs = require("node:fs");
const [sarifPath, language] = process.argv.slice(2);
const sarif = JSON.parse(fs.readFileSync(sarifPath, "utf8"));
const results = sarif.runs.flatMap((run) => run.results ?? []);
console.log(`${language}: ${results.length} result(s)`);
for (const result of results) {
  const location = result.locations?.[0]?.physicalLocation;
  const uri = location?.artifactLocation?.uri ?? "unknown";
  const line = location?.region?.startLine ?? "?";
  console.error(`${result.ruleId ?? "CodeQL"}: ${uri}:${line}: ${result.message?.text ?? "finding"}`);
}
process.exit(results.length === 0 ? 0 : 1);
NODE
        ' bash \
        "/codeql-cache/codeql-${CODEQL_VERSION}/codeql" \
        "${database}" \
        "${language}" \
        "${suite}" \
        "${sarif}" \
        "${THREADS}" \
        "${RAM_MB}" \
        "${MODEL_PACK_ROOT}" \
        "${MODEL_PACK}" >"${log_path}" 2>&1; then
        grep -F "${language}: " "${log_path}" | tail -n 1
    else
        echo "CodeQL ${language} analysis failed; final log output follows:" >&2
        tail -n 200 "${log_path}" >&2
        return 1
    fi
}

if (( $# == 0 )); then
    set -- python javascript-typescript
fi
for language in "$@"; do
    case "${language}" in
        python)
            run_language "python" "python-code-scanning.qls"
            ;;
        javascript-typescript)
            run_language "javascript-typescript" "javascript-code-scanning.qls"
            ;;
        *)
            echo "Unsupported CodeQL language: ${language}" >&2
            exit 2
            ;;
    esac
done

echo "CodeQL passed with no findings."
