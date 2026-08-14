#!/usr/bin/env bash
# Build a distribution APK only when an explicit non-debug signing identity is
# available. Passwords are read from the environment and are never echoed.

set -Eeuo pipefail

project_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly project_root
readonly android_dir="${project_root}/android"
readonly apk_path="${android_dir}/app/build/outputs/apk/release/app-release.apk"

die() {
    printf 'Release build refused: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

require_environment() {
    local name
    local missing=()

    for name in LEDGERLY_SIGNING_STORE_FILE STORE_PASSWORD KEY_ALIAS KEY_PASSWORD LEDGERLY_EXPECTED_CERT_SHA256; do
        if [[ -z "${!name:-}" ]]; then
            missing+=("${name}")
        fi
    done

    if ((${#missing[@]} > 0)); then
        printf 'Release build refused: required signing environment variable(s) are missing: %s\n' \
            "$(IFS=', '; printf '%s' "${missing[*]}")" >&2
        printf 'Set all five explicitly; this script will not fall back to Android Debug signing.\n' >&2
        exit 1
    fi
}

android_tool() {
    local tool="$1"
    local sdk_root candidate
    local -a sdk_roots=("${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "${HOME}/.android-sdk")

    if candidate="$(command -v "${tool}" 2>/dev/null)"; then
        printf '%s\n' "${candidate}"
        return 0
    fi

    for sdk_root in "${sdk_roots[@]}"; do
        [[ -n "${sdk_root}" && -d "${sdk_root}/build-tools" ]] || continue
        candidate="$(find "${sdk_root}/build-tools" -mindepth 2 -maxdepth 2 -type f -name "${tool}" -perm -u+x -print 2>/dev/null | sort -V | tail -n 1)"
        if [[ -n "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    die "could not find ${tool}; add Android SDK build-tools to PATH or set ANDROID_HOME"
}

require_environment

[[ -f "${LEDGERLY_SIGNING_STORE_FILE}" ]] || die "LEDGERLY_SIGNING_STORE_FILE does not name a regular file"
[[ -r "${LEDGERLY_SIGNING_STORE_FILE}" ]] || die "LEDGERLY_SIGNING_STORE_FILE is not readable"
[[ -x "${android_dir}/gradlew" ]] || die "Android Gradle wrapper is not executable"
require_command sha256sum

apksigner="$(android_tool apksigner)"
readonly apksigner
aapt="$(android_tool aapt)"
readonly aapt

printf 'Building a freshly signed release APK...\n'
rm -f -- "${apk_path}"
(
    cd "${android_dir}"
    # The Android module consumes these names. Keep the generic operator
    # inputs above separate so no password ever appears in command arguments.
    LEDGERLY_SIGNING_STORE_PASSWORD="${STORE_PASSWORD}" \
    LEDGERLY_SIGNING_KEY_ALIAS="${KEY_ALIAS}" \
    LEDGERLY_SIGNING_KEY_PASSWORD="${KEY_PASSWORD}" \
    ./gradlew --no-daemon --max-workers=1 \
        -Pkotlin.compiler.execution.strategy=in-process \
        '-Dorg.gradle.jvmargs=-Xmx384m -XX:+UseSerialGC -Dfile.encoding=UTF-8' \
        --rerun-tasks :app:assembleRelease
)

[[ -f "${apk_path}" ]] || die "Gradle completed without producing ${apk_path}"

verification="$("${apksigner}" verify --verbose --print-certs --min-sdk-version 26 "${apk_path}")" \
    || die "apksigner could not verify the release APK"

if grep -Eiq 'certificate DN:.*CN=Android Debug([,[:space:]]|$)' <<<"${verification}"; then
    die "APK is signed with Android Debug; stable distribution requires the external Ledgerly release keystore"
fi

certificate_lines="$(grep -E 'Signer #[0-9]+ certificate (DN|SHA-256 digest):' <<<"${verification}" || true)"
[[ -n "${certificate_lines}" ]] || die "apksigner did not report a signing certificate"
actual_cert_sha256="$(sed -n 's/.*certificate SHA-256 digest: //p' <<<"${verification}" | tr -d ':[:space:]' | tr '[:upper:]' '[:lower:]' | head -n 1)"
expected_cert_sha256="$(tr -d ':[:space:]' <<<"${LEDGERLY_EXPECTED_CERT_SHA256}" | tr '[:upper:]' '[:lower:]')"
[[ "${expected_cert_sha256}" =~ ^[0-9a-f]{64}$ ]] || die "LEDGERLY_EXPECTED_CERT_SHA256 must be a 64-hex certificate digest"
[[ "${actual_cert_sha256}" == "${expected_cert_sha256}" ]] || die "APK certificate does not match the expected Ledgerly release identity"

badging="$("${aapt}" dump badging "${apk_path}")" \
    || die "aapt could not inspect the release APK"
version_code="$(sed -n "s/.*versionCode='\([^']*\)'.*/\1/p" <<<"${badging}" | head -n 1)"
version_name="$(sed -n "s/.*versionName='\([^']*\)'.*/\1/p" <<<"${badging}" | head -n 1)"
[[ "${version_code}" =~ ^[0-9]+$ ]] || die "APK has no numeric versionCode"
[[ -n "${version_name}" ]] || die "APK has no versionName"
minimum_version_code="${LEDGERLY_MIN_VERSION_CODE:-1}"
[[ "${minimum_version_code}" =~ ^[0-9]+$ ]] || die "LEDGERLY_MIN_VERSION_CODE must be numeric"
(( version_code > minimum_version_code )) || die "versionCode ${version_code} must be greater than LEDGERLY_MIN_VERSION_CODE ${minimum_version_code}"

apk_sha256="$(sha256sum "${apk_path}" | awk '{print $1}')"

printf 'Release APK verified\n'
printf '  artifact: %s\n' "${apk_path}"
printf '  versionCode: %s\n' "${version_code}"
printf '  versionName: %s\n' "${version_name}"
printf '  certificate:\n    %s\n' "${certificate_lines//$'\n'/$'\n    '}"
printf '  sha256: %s\n' "${apk_sha256}"
