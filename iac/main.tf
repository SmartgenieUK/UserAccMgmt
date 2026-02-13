data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 5
  special = false
  upper   = false
}

resource "random_password" "postgres_admin" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

locals {
  env_slug = lower(var.env_name)

  name_suffix = "${local.env_slug}-${random_string.suffix.result}"

  common_tags = merge(
    {
      environment = local.env_slug
      managed_by  = "terraform"
      workload    = "auth-platform"
    },
    var.tags
  )

  redis_family = var.redis_sku == "Premium" ? "P" : "C"

  key_vault_name = substr(replace("kvauth${local.env_slug}${random_string.suffix.result}", "-", ""), 0, 24)

  storage_account_name = substr(replace("stauth${local.env_slug}${random_string.suffix.result}", "-", ""), 0, 24)
}

resource "azurerm_resource_group" "this" {
  name     = "rg-auth-${local.name_suffix}"
  location = var.location
  tags     = local.common_tags
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-auth-${local.name_suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

resource "azurerm_application_insights" "this" {
  name                = "appi-auth-${local.name_suffix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  workspace_id        = azurerm_log_analytics_workspace.this.id
  application_type    = "web"
  tags                = local.common_tags
}

resource "azurerm_postgresql_flexible_server" "this" {
  name                   = "psql-auth-${local.name_suffix}"
  resource_group_name    = azurerm_resource_group.this.name
  location               = azurerm_resource_group.this.location
  version                = "16"
  administrator_login    = var.postgres_admin_username
  administrator_password = random_password.postgres_admin.result
  storage_mb             = var.postgres_storage_mb
  sku_name               = var.postgres_sku

  backup_retention_days        = 7
  geo_redundant_backup_enabled = false
  public_network_access_enabled = var.postgres_public_network_access_enabled

  lifecycle {
    ignore_changes = [zone]
  }

  tags = local.common_tags
}

resource "azurerm_postgresql_flexible_server_configuration" "require_secure_transport" {
    name                = "require_secure_transport"
    server_id           = azurerm_postgresql_flexible_server.this.id
    value               = "on"
}

resource "azurerm_postgresql_flexible_server_configuration" "tls_min" {
    name                = "ssl_min_protocol_version"
    server_id           = azurerm_postgresql_flexible_server.this.id
    value               = "TLSv1.2"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "azure_services" {
  count = var.postgres_public_network_access_enabled && var.allow_azure_services ? 1 : 0

  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "allowed_ips" {
  for_each = var.postgres_public_network_access_enabled ? toset(var.postgres_allowed_ips) : toset([])

  name             = "allow-${replace(each.value, ".", "-")}"
  server_id        = azurerm_postgresql_flexible_server.this.id
  start_ip_address = each.value
  end_ip_address   = each.value
}

resource "azurerm_postgresql_flexible_server_database" "authdb" {
  name      = "authdb"
  server_id = azurerm_postgresql_flexible_server.this.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

resource "azurerm_redis_cache" "this" {
  name                          = "redis-auth-${local.name_suffix}"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  capacity                      = var.redis_capacity
  family                        = local.redis_family
  sku_name                      = var.redis_sku
  minimum_tls_version           = "1.2"
  non_ssl_port_enabled          = false
  public_network_access_enabled = true

  tags = local.common_tags
}

resource "azurerm_key_vault" "this" {
  name                          = local.key_vault_name
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  tenant_id                     = var.tenant_id
  sku_name                      = "standard"
  purge_protection_enabled      = false
  soft_delete_retention_days    = 7
  public_network_access_enabled = true

  tags = local.common_tags
}

resource "azurerm_key_vault_access_policy" "deployer" {
  key_vault_id = azurerm_key_vault.this.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = data.azurerm_client_config.current.object_id

  secret_permissions = [
    "Get",
    "List",
    "Set",
    "Delete",
    "Recover",
    "Purge"
  ]
}

locals {
  postgres_connection_string = format(
    "postgresql://%s:%s@%s:5432/%s?sslmode=require",
    var.postgres_admin_username,
    random_password.postgres_admin.result,
    azurerm_postgresql_flexible_server.this.fqdn,
    azurerm_postgresql_flexible_server_database.authdb.name
  )
}

resource "azurerm_key_vault_secret" "postgres_connection" {
  name         = "POSTGRES-CONNECTION-STRING"
  value        = local.postgres_connection_string
  key_vault_id = azurerm_key_vault.this.id

  depends_on = [azurerm_key_vault_access_policy.deployer]
}

resource "azurerm_key_vault_secret" "redis_hostname" {
  name         = "REDIS-HOSTNAME"
  value        = azurerm_redis_cache.this.hostname
  key_vault_id = azurerm_key_vault.this.id

  depends_on = [azurerm_key_vault_access_policy.deployer]
}

resource "azurerm_key_vault_secret" "redis_primary_key" {
  name         = "REDIS-PRIMARY-KEY"
  value        = azurerm_redis_cache.this.primary_access_key
  key_vault_id = azurerm_key_vault.this.id

  depends_on = [azurerm_key_vault_access_policy.deployer]
}

resource "azurerm_container_app_environment" "this" {
  count = var.enable_container_apps_env ? 1 : 0

  name                       = "cae-auth-${local.name_suffix}"
  location                   = azurerm_resource_group.this.location
  resource_group_name        = azurerm_resource_group.this.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  tags = local.common_tags
}

resource "azurerm_storage_account" "this" {
  count = var.enable_storage_account ? 1 : 0

  name                            = local.storage_account_name
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  public_network_access_enabled   = true
  allow_nested_items_to_be_public = false

  tags = local.common_tags
}
