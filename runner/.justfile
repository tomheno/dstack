# Justfile for building and uploading dstack runner and shim
#
# Run `just` to see all available commands
#
# Configuration:
# - DSTACK_SHIM_UPLOAD_VERSION: Version of the runner and shim to upload
# - DSTACK_SHIM_UPLOAD_S3_BUCKET: S3 bucket to upload binaries to
# - DSTACK_SHIM_UPLOAD_ARCH: Target CPU arch, amd64 or arm64 (default amd64)
#
# Build Process:
# - Runner and shim are built+uploaded for linux/$DSTACK_SHIM_UPLOAD_ARCH (default amd64)
# - Download URLs are arch-templated (dstack-{shim,runner}-linux-{arch}) so the server
#   routes the correct binary to each target host's architecture. An amd64 shim on an
#   arm64 host (or vice versa) fails to start with "Exec format error".
# - Use the *-all recipes to build/upload BOTH amd64 and arm64 in one shot.
#
# Development Workflows:
# - Local Development:
#   * Use build recipes to build binaries for local testing
#   * See README.md for instructions on running dstack server with local binaries
#   * No need to upload binaries for local development
#
# - Remote Development:
#   * Use upload recipes to build and upload binaries to S3
#   * See README.md for instructions on running dstack server with uploaded binaries
#   * Upload is required for testing with standard backends (including SSH fleets)

default:
    @just --list

# Version of the runner and shim to upload
export version := env("DSTACK_SHIM_UPLOAD_VERSION", "0.0.0")

# S3 bucket to upload binaries to
export s3_bucket := env("DSTACK_SHIM_UPLOAD_S3_BUCKET", "dstack-runner-downloads-stgn")

# Target CPU architecture (amd64 or arm64). Build + upload route by this so each host
# gets a matching binary; a cross-arch mismatch makes the shim fail with "Exec format error".
export arch := env("DSTACK_SHIM_UPLOAD_ARCH", "amd64")

# Download URLs (arch-templated)
export runner_download_url := "s3://" + s3_bucket + "/" + version + "/binaries/dstack-runner-linux-" + arch
export shim_download_url := "s3://" + s3_bucket + "/" + version + "/binaries/dstack-shim-linux-" + arch

# Shim build configuration
export shim_os := ""
export shim_arch := ""

# Build runner
[private]
build-runner-binary:
    #!/usr/bin/env bash
    set -e
    echo "Building runner for linux/$arch"
    cd {{source_directory()}}/cmd/runner && CGO_ENABLED=0 GOOS=linux GOARCH=$arch go build -ldflags "-X 'main.Version=$version' -extldflags '-static'"
    echo "Runner build complete!"

# Build shim
[private]
build-shim-binary:
    #!/usr/bin/env bash
    set -e
    cd {{source_directory()}}/cmd/shim
    if [ -n "$shim_os" ] && [ -n "$shim_arch" ]; then
        echo "Building shim for $shim_os/$shim_arch"
        if [ "$shim_os" = "linux" ] && [ "$(uname -s)" != "Linux" ]; then
            echo "WARNING: Cross-compiling to Linux, disabling CGO (DCGM unavailable)"
            CGO_ENABLED=0 GOOS=$shim_os GOARCH=$shim_arch go build -ldflags "-X 'main.Version=$version' -extldflags '-static'"
        else
            CGO_ENABLED=1 GOOS=$shim_os GOARCH=$shim_arch go build -ldflags "-X 'main.Version=$version'"
        fi
    else
        echo "Building shim for current platform"
        go build -ldflags "-X 'main.Version=$version' -extldflags '-static'"
    fi
    echo "Shim build (version: $version) complete!"

# Build both runner and shim
build-runner: build-runner-binary build-shim-binary
    echo "Build complete! linux/$arch binaries are in their respective cmd directories."

# Clean build artifacts
clean-runner:
    rm -f {{source_directory()}}/cmd/runner/runner
    rm -f {{source_directory()}}/cmd/shim/shim
    echo "Build artifacts cleaned!"

# Run tests for runner and shim
test-runner:
    cd {{source_directory()}} && go test -v ./...

# Validate shim is built for linux/$arch
[private]
validate-shim-binary:
    #!/usr/bin/env bash
    set -e
    case "$arch" in
        amd64) want="x86-64" ;;
        arm64) want="aarch64" ;;
        *) echo "Error: unsupported arch '$arch' (use amd64 or arm64)"; exit 1 ;;
    esac
    if ! file {{source_directory()}}/cmd/shim/shim | grep -q "ELF 64-bit LSB executable, $want"; then
        echo "Error: Shim must be built for linux/$arch for upload"
        exit 1
    fi

# Upload both runner and shim to S3 (for the arch in $arch)
upload-runner: upload-runner-binary upload-shim-binary

# Build runner + shim for BOTH linux/amd64 and linux/arm64
build-runner-all:
    DSTACK_SHIM_UPLOAD_ARCH=amd64 just build-runner
    DSTACK_SHIM_UPLOAD_ARCH=arm64 just build-runner

# Upload runner + shim for BOTH linux/amd64 and linux/arm64 (server routes per target arch)
upload-runner-all:
    DSTACK_SHIM_UPLOAD_ARCH=amd64 just upload-runner
    DSTACK_SHIM_UPLOAD_ARCH=arm64 just upload-runner

# Upload runner to S3
[private]
upload-runner-binary:
    #!/usr/bin/env bash
    set -e
    just build-runner-binary
    aws s3 cp {{source_directory()}}/cmd/runner/runner "{{runner_download_url}}" --acl public-read
    echo "Uploaded runner to S3"

# Upload shim to S3
[private]
upload-shim-binary:
    #!/usr/bin/env bash
    set -e
    just --set shim_os linux --set shim_arch "$arch" build-shim-binary
    just validate-shim-binary
    aws s3 cp {{source_directory()}}/cmd/shim/shim "{{shim_download_url}}" --acl public-read
    echo "Uploaded shim to S3"
