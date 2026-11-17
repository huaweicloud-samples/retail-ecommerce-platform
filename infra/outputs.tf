data "huaweicloud_apig_groups" "retail" {
  instance_id = local.apig_instance_id
  group_id    = huaweicloud_apig_group.retail.id
}

output "apig_instance_id" {
  description = "本应用使用的 APIG 专享实例 ID。"
  value       = local.apig_instance_id
}

output "apig_env_id" {
  description = "本应用使用的 APIG 环境 ID。"
  value       = local.apig_env_id
}

output "api_base_url" {
  description = "APIG 分组调试域名根地址。"
  value       = "https://${data.huaweicloud_apig_groups.retail.groups[0].sl_domain}"
}

output "api_invoke_url" {
  description = "REST API 根地址。"
  value       = "https://${data.huaweicloud_apig_groups.retail.groups[0].sl_domain}"
}

output "api_function_name" {
  description = "REST ingress FunctionGraph 函数名。"
  value       = huaweicloud_fgs_function.api.name
}

output "consumer_function_name" {
  description = "EG consumer FunctionGraph 函数名。"
  value       = huaweicloud_fgs_function.consumer.name
}

output "api_function_urn" {
  description = "REST ingress FunctionGraph 函数 URN。"
  value       = huaweicloud_fgs_function.api.urn
}

output "consumer_function_urn" {
  description = "EG consumer FunctionGraph 函数 URN。"
  value       = huaweicloud_fgs_function.consumer.urn
}

output "event_channel_id" {
  description = "本应用 EG 自定义通道 ID。"
  value       = huaweicloud_eg_custom_event_channel.retail.id
}

output "event_source_name" {
  description = "本应用 EG 自定义事件源名。"
  value       = huaweicloud_eg_custom_event_source.retail.name
}

output "event_subscription_id" {
  description = "本应用 EG 订阅 ID。"
  value       = huaweicloud_eg_event_subscription.retail.id
}

output "log_group_id" {
  description = "应用 LTS 日志组 ID。"
  value       = huaweicloud_lts_group.retail.id
}

output "api_log_stream_id" {
  description = "REST ingress LTS 日志流 ID。"
  value       = huaweicloud_lts_stream.api.id
}

output "consumer_log_stream_id" {
  description = "EG consumer LTS 日志流 ID。"
  value       = huaweicloud_lts_stream.consumer.id
}

output "repository_mode" {
  description = "函数运行时数据访问模式。"
  value       = var.repository_mode
}

output "data_namespace" {
  description = "应用数据命名空间。"
  value       = var.namespace
}
