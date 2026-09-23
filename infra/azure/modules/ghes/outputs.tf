output "private_ip" {
  value = azurerm_network_interface.ghes.private_ip_address
}

output "management_url" {
  value = "https://\${var.hostname}:8443"
}

output "server_url" {
  value = "https://\${var.hostname}"
}

output "actions_storage_account" {
  value = var.actions_enabled ? azurerm_storage_account.actions[0].name : null
}
