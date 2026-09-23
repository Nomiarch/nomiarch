locals {
  image       = split(":", var.image_urn)
  storage_raw = lower(replace("${var.prefix}ghes", "-", ""))
  storage     = substr(local.storage_raw, 0, min(length(local.storage_raw), 24))
  vnet_id     = join("/", slice(split("/", var.subnet_id), 0, 9))
}

resource "azurerm_marketplace_agreement" "ghes" {
  publisher = local.image[0]
  offer     = local.image[1]
  plan      = local.image[2]
}

resource "azurerm_resource_group" "ghes" {
  name     = "${var.prefix}-ghes-rg"
  location = var.location
  tags     = var.tags
}

resource "azurerm_network_security_group" "ghes" {
  name                = "${var.prefix}-ghes-nsg"
  location            = var.location
  resource_group_name = azurerm_resource_group.ghes.name
  tags                = var.tags

  security_rule {
    name                       = "https-from-private-network"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "management-from-administration"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["8443", "122"]
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
}

resource "azurerm_network_interface" "ghes" {
  name                = "${var.prefix}-ghes-nic"
  location            = var.location
  resource_group_name = azurerm_resource_group.ghes.name
  tags                = var.tags

  ip_configuration {
    name                          = "private"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_network_interface_security_group_association" "ghes" {
  network_interface_id      = azurerm_network_interface.ghes.id
  network_security_group_id = azurerm_network_security_group.ghes.id
}

resource "azurerm_linux_virtual_machine" "ghes" {
  name                            = "${var.prefix}-ghes"
  location                        = var.location
  resource_group_name             = azurerm_resource_group.ghes.name
  size                            = var.vm_size
  admin_username                  = "ghesadmin"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.ghes.id]
  tags                            = var.tags

  plan {
    publisher = local.image[0]
    product   = local.image[1]
    name      = local.image[2]
  }

  source_image_reference {
    publisher = local.image[0]
    offer     = local.image[1]
    sku       = local.image[2]
    version   = local.image[3]
  }

  admin_ssh_key {
    username   = "ghesadmin"
    public_key = var.ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = 400
  }

  depends_on = [
    azurerm_marketplace_agreement.ghes,
    azurerm_network_interface_security_group_association.ghes
  ]
}

resource "azurerm_managed_disk" "ghes_data" {
  name                 = "${var.prefix}-ghes-data"
  location             = var.location
  resource_group_name  = azurerm_resource_group.ghes.name
  storage_account_type = "Premium_LRS"
  create_option        = "Empty"
  disk_size_gb         = 500
  tags                 = var.tags
}

resource "azurerm_virtual_machine_data_disk_attachment" "ghes_data" {
  managed_disk_id    = azurerm_managed_disk.ghes_data.id
  virtual_machine_id = azurerm_linux_virtual_machine.ghes.id
  lun                = 0
  caching            = "ReadWrite"
}

resource "azurerm_storage_account" "actions" {
  count                            = var.actions_enabled ? 1 : 0
  name                             = local.storage
  resource_group_name              = azurerm_resource_group.ghes.name
  location                         = var.location
  account_tier                     = "Standard"
  account_replication_type         = "LRS"
  min_tls_version                  = "TLS1_2"
  public_network_access_enabled    = false
  shared_access_key_enabled        = false
  allow_nested_items_to_be_public = false
  tags                             = var.tags
}

resource "azurerm_private_dns_zone" "blob" {
  count               = var.actions_enabled ? 1 : 0
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.ghes.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  count                 = var.actions_enabled ? 1 : 0
  name                  = "${var.prefix}-ghes-actions"
  resource_group_name   = azurerm_resource_group.ghes.name
  private_dns_zone_name = azurerm_private_dns_zone.blob[0].name
  virtual_network_id    = local.vnet_id
  registration_enabled  = false
  tags                  = var.tags
}

resource "azurerm_private_endpoint" "actions" {
  count               = var.actions_enabled ? 1 : 0
  name                = "${var.prefix}-ghes-actions-pe"
  location            = var.location
  resource_group_name = azurerm_resource_group.ghes.name
  subnet_id           = var.subnet_id
  tags                = var.tags

  private_service_connection {
    name                           = "${var.prefix}-ghes-actions"
    private_connection_resource_id = azurerm_storage_account.actions[0].id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob[0].id]
  }
}
