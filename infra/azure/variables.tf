variable "subscription_id" { type = string }
variable "location" { type = string }
variable "prefix" { type = string }
variable "run_id" { type = string }
variable "expires_at" { type = string }
variable "vm_size" { type = string }
variable "ssh_user" { type = string }
variable "ssh_public_key" { type = string }
variable "ssh_host_public_key" { type = string }
variable "ssh_host_private_key" {
  type      = string
  sensitive = true
}
variable "admin_cidr" { type = string }
variable "disk_gib" { type = number }
variable "subnet_id" {
  type    = string
  default = ""
}
variable "vnet_cidr" {
  type    = string
  default = "10.88.0.0/16"
}
variable "subnet_cidr" {
  type    = string
  default = "10.88.1.0/24"
}
variable "image" {
  type = object({ publisher = string, offer = string, sku = string, version = string })
  validation {
    condition     = var.image.version != "latest"
    error_message = "Use an exact admitted Ubuntu 24.04 image version."
  }
}
