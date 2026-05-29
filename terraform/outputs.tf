output "event_bus_name" { value = module.eventing.event_bus_name }
output "signals_bucket_name" { value = module.signals_bucket.bucket_name }
output "signal_collector_lambda" { value = module.signal_collector_lambda.lambda_name }
output "decision_engine_lambda" { value = module.decision_engine_lambda.lambda_name }
output "outcome_validator_lambda" { value = module.outcome_validator_lambda.lambda_name }

output "cluster_name" { value = module.eks.cluster_name }
output "cluster_endpoint" { value = module.eks.cluster_endpoint }
output "prometheus_query_url" { value = local.prometheus_query_url_effective }

output "signals_table_name" { value = module.signals_table.table_name }

output "classifier_agent_lambda" { value = module.classifier_agent.lambda_name }
output "root_cause_agent_lambda" { value = module.root_cause_agent.lambda_name }
output "action_planner_lambda" { value = module.action_planner.lambda_name }
output "confidence_scorer_lambda" { value = module.confidence_scorer_agent.lambda_name }
