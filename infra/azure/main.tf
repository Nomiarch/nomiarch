terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 4.49.0"
    }
  }
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
    virtual_machine {
      delete_os_disk_on_deletion = true
    }
  }
}

locals {
  tags = { "nomiarch-run" = var.run_id, "nomiarch-expires" = var.expires_at, "managed-by" = "nomiarch" }
}

resource "azurerm_resource_group" "core" {
  name     = "${var.prefix}-rg"
  location = var.location
  tags     = local.tags
}

resource "azurerm_virtual_network" "core" {
  count               = var.subnet_id == "" ? 1 : 0
  name                = "${var.prefix}-vnet"
  location            = var.location
  resource_group_name = azurerm_resource_group.core.name
  address_space       = [var.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_subnet" "core" {
  count                           = var.subnet_id == "" ? 1 : 0
  name                            = "core"
  resource_group_name             = azurerm_resource_group.core.name
  virtual_network_name            = azurerm_virtual_network.core[0].name
  address_prefixes                = [var.subnet_cidr]
  default_outbound_access_enabled = false
}

resource "azurerm_network_security_group" "core" {
  name                = "${var.prefix}-nsg"
  location            = var.location
  resource_group_name = azurerm_resource_group.core.name
  tags                = local.tags

  security_rule {
    name                       = "ssh-from-administration"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = var.admin_cidr
    destination_address_prefix = "*"
  }
  security_rule {
    name                       = "deny-other-inbound"
    priority                   = 200
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
  security_rule {
    name                       = "azure-platform-agent"
    priority                   = 100
    direction                  = "Outbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "32526"]
    source_address_prefix      = "*"
    destination_address_prefix = "168.63.129.16/32"
  }
  security_rule {
    name                       = "deny-other-outbound"
    priority                   = 200
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_interface" "core" {
  name                = "${var.prefix}-nic"
  location            = var.location
  resource_group_name = azurerm_resource_group.core.name
  tags                = local.tags
  ip_configuration {
    name                          = "private"
    subnet_id                     = var.subnet_id != "" ? var.subnet_id : azurerm_subnet.core[0].id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_network_interface_security_group_association" "core" {
  network_interface_id      = azurerm_network_interface.core.id
  network_security_group_id = azurerm_network_security_group.core.id
}

resource "azurerm_linux_virtual_machine" "core" {
  name                            = "${var.prefix}-vm"
  location                        = var.location
  resource_group_name             = azurerm_resource_group.core.name
  size                            = var.vm_size
  admin_username                  = var.ssh_user
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.core.id]
  provision_vm_agent              = true
  allow_extension_operations      = false
  patch_mode                      = "ImageDefault"
  patch_assessment_mode           = "ImageDefault"
  tags                            = local.tags
  custom_data = base64encode(join("\n", ["#cloud-config", yamlencode({
    package_update  = false
    package_upgrade = false
    ssh_pwauth      = false
    ssh_keys = {
      ed25519_private = var.ssh_host_private_key
      ed25519_public  = var.ssh_host_public_key
    }
  })]))
  admin_ssh_key {
    username   = var.ssh_user
    public_key = var.ssh_public_key
  }
  os_disk {
    name                 = "${var.prefix}-os"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 32
  }
  source_image_reference {
    publisher = var.image.publisher
    offer     = var.image.offer
    sku       = var.image.sku
    version   = var.image.version
  }
  depends_on = [azurerm_network_interface_security_group_association.core]
}

resource "azurerm_managed_disk" "data" {
  name                 = "${var.prefix}-data"
  location             = var.location
  resource_group_name  = azurerm_resource_group.core.name
  storage_account_type = "Standard_LRS"
  create_option        = "Empty"
  disk_size_gb         = var.disk_gib
  tags                 = local.tags
}

resource "azurerm_virtual_machine_data_disk_attachment" "data" {
  managed_disk_id    = azurerm_managed_disk.data.id
  virtual_machine_id = azurerm_linux_virtual_machine.core.id
  lun                = 0
  caching            = "ReadWrite"
}

output "inventory" {
  value = {
    host           = azurerm_network_interface.core.private_ip_address
    vm_id          = azurerm_linux_virtual_machine.core.id
    resource_group = azurerm_resource_group.core.name
    data_disk_id   = azurerm_managed_disk.data.id
    data_device    = "/dev/disk/azure/scsi1/lun0"
    mode           = "azure-restricted"
  }
}
