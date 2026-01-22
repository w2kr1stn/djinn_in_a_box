#!/bin/bash
# Microsoft Azure CLI
set -e

# Install dependencies
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends gnupg lsb-release
sudo rm -rf /var/lib/apt/lists/*

# Add Microsoft signing key and repo
curl -sL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor | sudo tee /usr/share/keyrings/microsoft-archive-keyring.gpg > /dev/null
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-archive-keyring.gpg] https://packages.microsoft.com/repos/azure-cli/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/azure-cli.list

# Install Azure CLI
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends azure-cli
sudo rm -rf /var/lib/apt/lists/*

az version --output tsv | head -1
