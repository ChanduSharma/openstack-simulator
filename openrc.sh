#!/usr/bin/env bash
# Sourceable credentials for OpenStack-Simulator:  source openrc.sh
#
# Works with python-openstackclient, the OpenStack SDK, and the Terraform
# OpenStack provider (which reads the same OS_* variables).

# --- identity -----------------------------------------------------------------------
export OS_AUTH_URL="${OS_AUTH_URL:-http://127.0.0.1:5000/v3}"
export OS_IDENTITY_API_VERSION=3
export OS_USERNAME="${OS_USERNAME:-admin}"
export OS_PASSWORD="${OS_PASSWORD:-secret}"
export OS_PROJECT_NAME="${OS_PROJECT_NAME:-admin}"
export OS_USER_DOMAIN_NAME="${OS_USER_DOMAIN_NAME:-Default}"
export OS_PROJECT_DOMAIN_NAME="${OS_PROJECT_DOMAIN_NAME:-Default}"
export OS_REGION_NAME="${OS_REGION_NAME:-RegionOne}"
export OS_INTERFACE=public
export OS_AUTH_TYPE=password

# --- api versions -------------------------------------------------------------------
export OS_COMPUTE_API_VERSION=2.79
export OS_VOLUME_API_VERSION=3
export OS_IMAGE_API_VERSION=2
export OS_NETWORK_API_VERSION=2.0
export OS_PLACEMENT_API_VERSION=1.36
export OS_LOADBALANCER_API_VERSION=2.0
export OS_OBJECT_API_VERSION=1
export OS_RATING_API_VERSION=1

# --- simulator-only endpoints (not part of the Keystone catalog) ----------------------
export OPENSTACK_SIMULATOR_SCENARIOS_URL="${OPENSTACK_SIMULATOR_SCENARIOS_URL:-http://127.0.0.1:8999/v1/scenarios}"
export OPENSTACK_SIMULATOR_DASHBOARD_URL="${OPENSTACK_SIMULATOR_DASHBOARD_URL:-http://127.0.0.1:10000/}"

# Plain HTTP against loopback: no TLS to verify.
unset OS_CACERT
export OS_INSECURE=true

echo "OpenStack-Simulator credentials loaded:"
echo "  auth url : ${OS_AUTH_URL}"
echo "  identity : ${OS_USERNAME}@${OS_PROJECT_NAME} (domain ${OS_USER_DOMAIN_NAME})"
echo "  dashboard: ${OPENSTACK_SIMULATOR_DASHBOARD_URL}"
echo "  scenarios: ${OPENSTACK_SIMULATOR_SCENARIOS_URL}"
