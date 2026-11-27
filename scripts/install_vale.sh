#!/usr/bin/env bash
#
# Download the pinned Vale release into .vale/bin/ and verify its checksum.

set -o nounset
set -o pipefail

if [[ ${DEBUG:-0} == "1" ]]; then
    set -o xtrace
fi

VALE_VERSION="3.24.0"
SHA256_LINUX_X86_64="867534ddb678abca7f214bc4706ead0922bd5252ffe8035fc227013f66c8b214"
SHA256_LINUX_ARM64="104cbd349279e50c4cb40adb70adf345d93cc4a3a0191f9e68bdccb97086b582"
SHA256_MACOS_X86_64="eeb39e86f1daac27cc2a77fef3af1824d50559bdc22329fb25fb921c80c79472"
SHA256_MACOS_ARM64="87b513f26499f6657c15cf7175ba5b058a7c1ff2bb2d4e07265db2dd43451e44"

error() { printf '✗ %s\n' "$*" >&2; }
success() { printf '✓ %s\n' "$*" >&2; }
info() { printf 'ℹ %s\n' "$*" >&2; }
debug() { [[ ${DEBUG:-0} == "1" ]] && printf '[DEBUG] %s\n' "$*" >&2 || true; }

need_cmd() {
    command -v "$1" > /dev/null 2>&1 || {
        error "Missing required command: $1"
        exit 2
    }
}

usage() {
    cat << EOF
install_vale.sh [OPTIONS]

Download Vale ${VALE_VERSION} for this machine into .vale/bin/ of the
repository. The archive is unpacked only when its SHA-256 matches the value
pinned in this script.

Options:
  -f, --force   Download again even when .vale/bin/vale is already ${VALE_VERSION}
  -h, --help    Show this help

Examples:
  bash scripts/install_vale.sh
  DEBUG=1 bash scripts/install_vale.sh --force
EOF
    exit "${1:-0}"
}

TMP_DIR=""

# Remove the download directory, if one was created.
cleanup() {
    [[ -z ${TMP_DIR} ]] || rm -rf "${TMP_DIR}"
}

# Print the SHA-256 of a file with whichever tool the system has.
sha256_of() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        shasum -a 256 "$1" | cut -d ' ' -f 1
    fi
}

# Set ARCHIVE and EXPECTED_SHA256 for this system and architecture.
select_release() {
    local system machine
    system="$(uname -s)"
    machine="$(uname -m)"
    debug "system=${system} machine=${machine}"
    case "${system}/${machine}" in
        Linux/x86_64)
            ARCHIVE="vale_${VALE_VERSION}_Linux_64-bit.tar.gz"
            EXPECTED_SHA256="${SHA256_LINUX_X86_64}"
            ;;
        Linux/aarch64 | Linux/arm64)
            ARCHIVE="vale_${VALE_VERSION}_Linux_arm64.tar.gz"
            EXPECTED_SHA256="${SHA256_LINUX_ARM64}"
            ;;
        Darwin/x86_64)
            ARCHIVE="vale_${VALE_VERSION}_macOS_64-bit.tar.gz"
            EXPECTED_SHA256="${SHA256_MACOS_X86_64}"
            ;;
        Darwin/arm64)
            ARCHIVE="vale_${VALE_VERSION}_macOS_arm64.tar.gz"
            EXPECTED_SHA256="${SHA256_MACOS_ARM64}"
            ;;
        *)
            error "No pinned Vale release for ${system} ${machine}"
            exit 1
            ;;
    esac
}

main() {
    local force=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h | --help) usage 0 ;;
            -f | --force)
                force=1
                shift
                ;;
            -*)
                error "Unknown option: $1"
                usage 2
                ;;
            *)
                error "Unexpected argument: $1"
                usage 2
                ;;
        esac
    done

    need_cmd curl
    need_cmd tar
    need_cmd uname
    command -v sha256sum > /dev/null 2>&1 || need_cmd shasum

    local repo_root bin_dir
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
    bin_dir="${repo_root}/.vale/bin"

    if [[ ${force} -eq 0 && -x "${bin_dir}/vale" ]]; then
        local installed
        installed="$("${bin_dir}/vale" --version 2> /dev/null)"
        if [[ ${installed} == "vale version ${VALE_VERSION}" ]]; then
            info "Vale ${VALE_VERSION} is already at ${bin_dir}/vale"
            exit 0
        fi
    fi

    ARCHIVE=""
    EXPECTED_SHA256=""
    select_release

    TMP_DIR="$(mktemp -d)" || {
        error "Cannot create a temporary directory"
        exit 1
    }
    trap cleanup EXIT
    local tmp_dir="${TMP_DIR}"

    local url="https://github.com/vale-cli/vale/releases/download/v${VALE_VERSION}/${ARCHIVE}"
    info "Downloading ${url}"
    curl -fsSL -o "${tmp_dir}/${ARCHIVE}" "${url}" || {
        error "Download failed: ${url}"
        exit 1
    }

    local actual_sha256
    actual_sha256="$(sha256_of "${tmp_dir}/${ARCHIVE}")"
    debug "expected=${EXPECTED_SHA256} actual=${actual_sha256}"
    if [[ ${actual_sha256} != "${EXPECTED_SHA256}" ]]; then
        error "Checksum mismatch for ${ARCHIVE}: not unpacking"
        error "expected ${EXPECTED_SHA256}"
        error "got      ${actual_sha256}"
        exit 1
    fi

    mkdir -p "${bin_dir}" || exit 1
    tar -xzf "${tmp_dir}/${ARCHIVE}" -C "${tmp_dir}" vale || {
        error "Cannot unpack ${ARCHIVE}"
        exit 1
    }
    mv "${tmp_dir}/vale" "${bin_dir}/vale" || exit 1
    chmod +x "${bin_dir}/vale"
    success "Installed $("${bin_dir}/vale" --version) at ${bin_dir}/vale"
}

main "$@"
