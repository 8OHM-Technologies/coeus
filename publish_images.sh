#!/usr/bin/env bash

set -euo pipefail

# ANSI color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
TAG="latest"
BUILD_ONLY=false
DRY_RUN=false
REGISTRY="ghcr.io"
NAMESPACE="8ohm-technologies"

show_usage() {
    echo "Usage: $0 [options] [TAG]"
    echo ""
    echo "Builds, tags, and pushes Coeus Docker images to GitHub Container Registry (GHCR)."
    echo ""
    echo "Arguments:"
    echo "  TAG                  The tag to apply to the images (default: 'latest')"
    echo ""
    echo "Options:"
    echo "  -b, --build-only     Build and tag images locally, but do not push to registry"
    echo "  -d, --dry-run        Print commands that would be executed without running them"
    echo "  -h, --help           Show this help message and exit"
    echo ""
    echo "Example:"
    echo "  $0 v1.0.0"
    echo "  $0 --build-only"
}

# Parse options
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_usage
            exit 0
            ;;
        -b|--build-only)
            BUILD_ONLY=true
            shift
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -*)
            echo -e "${RED}Error: Unknown option $1${NC}" >&2
            show_usage
            exit 1
            ;;
        *)
            TAG="$1"
            shift
            # If multiple arguments are passed, show usage
            if [[ $# -gt 0 ]]; then
                echo -e "${RED}Error: Too many arguments.${NC}" >&2
                show_usage
                exit 1
            fi
            ;;
    esac
done

echo -e "${BLUE}=== Coeus Docker Build & Publish Script ===${NC}"
echo -e "Registry:   ${YELLOW}${REGISTRY}${NC}"
echo -e "Namespace:  ${YELLOW}${NAMESPACE}${NC}"
echo -e "Tag:        ${YELLOW}${TAG}${NC}"
echo -e "Build Only: ${YELLOW}${BUILD_ONLY}${NC}"
echo -e "Dry Run:    ${YELLOW}${DRY_RUN}${NC}"
echo ""

# Helper function to run commands or print them
run_cmd() {
    local cmd="$*"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo -e "${YELLOW}[DRY RUN]${NC} $cmd"
    else
        echo -e "${BLUE}Running:${NC} $cmd"
        eval "$cmd"
    fi
}

# Verify Docker is running
if [[ "$DRY_RUN" == "false" ]]; then
    if ! docker info >/dev/null 2>&1; then
        echo -e "${RED}Error: Docker daemon is not running or not accessible.${NC}" >&2
        exit 1
    fi
fi

# Define the build configurations:
# format: "Dockerfile:ImageName"
images=(
    "Dockerfile.app:coeus-app"
    "Dockerfile.worker:coeus-dagster-worker"
    "Dockerfile.dagster:coeus-dagster-webserver"
    "Dockerfile.scraper:coeus-scraper"
    "Dockerfile.extractor:coeus-extractor"
)

# 1. Build and Tag Images
for item in "${images[@]}"; do
    dockerfile="${item%%:*}"
    image_name="${item##*:}"
    full_tag="${REGISTRY}/${NAMESPACE}/${image_name}:${TAG}"

    echo -e "\n${GREEN}---> Building ${image_name} from ${dockerfile}...${NC}"
    
    if [[ ! -f "$dockerfile" ]]; then
        echo -e "${RED}Error: Dockerfile '${dockerfile}' not found.${NC}" >&2
        exit 1
    fi

    # Build and tag
    run_cmd "docker build -t ${full_tag} -f ${dockerfile} ."

    # Special handling for Dagster Daemon which shares Dockerfile.dagster
    if [[ "$image_name" == "coeus-dagster-webserver" ]]; then
        daemon_tag="${REGISTRY}/${NAMESPACE}/coeus-dagster-daemon:${TAG}"
        echo -e "${GREEN}---> Tagging coeus-dagster-daemon from built webserver image...${NC}"
        run_cmd "docker tag ${full_tag} ${daemon_tag}"
    fi
done

# 2. Push Images (if not build-only)
if [[ "$BUILD_ONLY" == "false" ]]; then
    echo -e "\n${GREEN}=== Pushing images to ${REGISTRY} ===${NC}"
    
    # Optional login warning
    if [[ "$DRY_RUN" == "false" ]]; then
        echo -e "${YELLOW}Note: Ensure you are authenticated to ${REGISTRY} (e.g. via 'docker login ${REGISTRY}').${NC}"
    fi

    for item in "${images[@]}"; do
        image_name="${item##*:}"
        full_tag="${REGISTRY}/${NAMESPACE}/${image_name}:${TAG}"
        
        echo -e "\n${GREEN}---> Pushing ${image_name}:${TAG}...${NC}"
        run_cmd "docker push ${full_tag}"
        
        # Also push daemon
        if [[ "$image_name" == "coeus-dagster-webserver" ]]; then
            daemon_tag="${REGISTRY}/${NAMESPACE}/coeus-dagster-daemon:${TAG}"
            echo -e "\n${GREEN}---> Pushing coeus-dagster-daemon:${TAG}...${NC}"
            run_cmd "docker push ${daemon_tag}"
        fi
    done
    
    echo -e "\n${GREEN}=== Pushed all images successfully! ===${NC}"
else
    echo -e "\n${YELLOW}=== Build only selected. Skipping push to registry. ===${NC}"
fi
