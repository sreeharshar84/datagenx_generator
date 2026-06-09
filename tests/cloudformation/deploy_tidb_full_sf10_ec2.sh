#!/usr/bin/env bash
# Deploy an EC2 runner and start full-size TiDB SF=10 verification through SSM.

set -Eeuo pipefail

cd "$(dirname "$0")/../.."

STACK_NAME="${STACK_NAME:-datagenx-tidb-full-sf10}"
AWS_REGION="${AWS_REGION:-$(aws configure get region)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m7i.2xlarge}"
VOLUME_SIZE_GIB="${VOLUME_SIZE_GIB:-300}"
RUN_PROFILE="${RUN_PROFILE:-all}"
TPCH_SCALE_FACTOR="${TPCH_SCALE_FACTOR:-10}"
TPCDS_SCALE_FACTOR="${TPCDS_SCALE_FACTOR:-10}"
ARTIFACT_PREFIX="${ARTIFACT_PREFIX:-full-sf${TPCH_SCALE_FACTOR}}"
SCHEMA_NAMESPACE="${SCHEMA_NAMESPACE:-}"
REUSE_SOURCE_FILES="${REUSE_SOURCE_FILES:-0}"
REUSE_LOADED_SOURCE_SCHEMA="${REUSE_LOADED_SOURCE_SCHEMA:-0}"
DATAGENX_APPLY_BENCHMARK_FK_DDL="${DATAGENX_APPLY_BENCHMARK_FK_DDL:-1}"
TEMPLATE_FILE="${TEMPLATE_FILE:-tests/cloudformation/tidb-full-sf10.yml}"
TIDB_ENV_FILE="${TIDB_ENV_FILE:-.env}"
TIDB_ENV_PARAM_NAME="${TIDB_ENV_PARAM_NAME:-/datagenx/$STACK_NAME/tidb-env}"

ARCHIVE="/tmp/datagenx_generator_${STACK_NAME}.tgz"
SSM_PARAMS="/tmp/datagenx_ssm_${STACK_NAME}.json"
SSM_PUT_PARAM="/tmp/datagenx_ti_env_param_${STACK_NAME}.json"

require_aws_identity() {
    aws sts get-caller-identity --region "$AWS_REGION" >/dev/null
}

stack_output() {
    local key="$1"
    aws cloudformation describe-stacks \
        --region "$AWS_REGION" \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue | [0]" \
        --output text
}

package_repo() {
    echo "Packaging current repository into $ARCHIVE"
    tar -czf "$ARCHIVE" \
        --exclude=".git" \
        --exclude=".env" \
        --exclude=".venv" \
        --exclude="__pycache__" \
        --exclude=".DS_Store" \
        --exclude="*.DS_Store" \
        --exclude="generated" \
        --exclude="results" \
        --exclude="messages.md" \
        --exclude="papers" \
        .
}

deploy_stack() {
    echo "Deploying CloudFormation stack $STACK_NAME in $AWS_REGION"
    aws cloudformation deploy \
        --region "$AWS_REGION" \
        --stack-name "$STACK_NAME" \
        --template-file "$TEMPLATE_FILE" \
        --capabilities CAPABILITY_IAM \
        --parameter-overrides \
            InstanceType="$INSTANCE_TYPE" \
            VolumeSizeGiB="$VOLUME_SIZE_GIB"
}

wait_for_ssm() {
    local instance_id="$1"
    echo "Waiting for SSM to see instance $instance_id"
    for _ in $(seq 1 120); do
        if aws ssm describe-instance-information \
            --region "$AWS_REGION" \
            --filters "Key=InstanceIds,Values=$instance_id" \
            --query "InstanceInformationList[0].PingStatus" \
            --output text 2>/dev/null | grep -q Online; then
            return
        fi
        sleep 5
    done
    echo "Timed out waiting for SSM instance registration" >&2
    exit 1
}

upload_repo() {
    local bucket="$1"
    echo "Uploading source archive to s3://$bucket/artifacts/datagenx_generator.tgz"
    aws s3 cp \
        --region "$AWS_REGION" \
        "$ARCHIVE" \
        "s3://$bucket/artifacts/datagenx_generator.tgz"
}

upload_tidb_env() {
    if [[ ! -f "$TIDB_ENV_FILE" ]]; then
        echo "ERROR: TiDB env file not found: $TIDB_ENV_FILE" >&2
        exit 1
    fi

    echo "Uploading TiDB Cloud credentials to SSM Parameter Store: $TIDB_ENV_PARAM_NAME"
    python3 - "$SSM_PUT_PARAM" "$TIDB_ENV_PARAM_NAME" "$TIDB_ENV_FILE" <<'PY'
import json
import sys
from pathlib import Path

out, name, env_file = sys.argv[1:]
Path(out).write_text(json.dumps({
    "Name": name,
    "Type": "SecureString",
    "Value": Path(env_file).read_text(),
    "Overwrite": True,
}))
PY
    aws ssm put-parameter \
        --region "$AWS_REGION" \
        --cli-input-json "file://$SSM_PUT_PARAM" \
        >/dev/null
    rm -f "$SSM_PUT_PARAM"
}

start_remote_run() {
    local instance_id="$1"
    local bucket="$2"
    local tidb_env_param_name="$3"

    python3 - "$SSM_PARAMS" "$bucket" "$AWS_REGION" "$RUN_PROFILE" "$TPCH_SCALE_FACTOR" "$TPCDS_SCALE_FACTOR" "$ARTIFACT_PREFIX" "$SCHEMA_NAMESPACE" "$REUSE_SOURCE_FILES" "$REUSE_LOADED_SOURCE_SCHEMA" "$DATAGENX_APPLY_BENCHMARK_FK_DDL" "$tidb_env_param_name" <<'PY'
import json
import sys

(
    path,
    bucket,
    region,
    profile,
    tpch_sf,
    tpcds_sf,
    artifact_prefix,
    schema_namespace,
    reuse_source_files,
    reuse_loaded_source_schema,
    apply_benchmark_fk_ddl,
    tidb_env_param_name,
) = sys.argv[1:]
commands = [
    "set -euo pipefail",
    "mkdir -p /opt/datagenx-run/work /opt/datagenx-run/logs /opt/datagenx-run/results",
    f"aws s3 cp s3://{bucket}/artifacts/datagenx_generator.tgz /opt/datagenx-run/work/datagenx_generator.tgz --region {region}",
    f"aws ssm get-parameter --region {region} --with-decryption --name '{tidb_env_param_name}' --query Parameter.Value --output text > /opt/datagenx-run/tidb-cloud.env",
    "chmod 600 /opt/datagenx-run/tidb-cloud.env",
    "rm -rf /opt/datagenx-run/work/datagenx_generator",
    "mkdir -p /opt/datagenx-run/work/datagenx_generator",
    "tar -xzf /opt/datagenx-run/work/datagenx_generator.tgz -C /opt/datagenx-run/work/datagenx_generator",
    "cd /opt/datagenx-run/work/datagenx_generator",
    "chmod +x tests/cloudformation/run_tidb_full_sf10_remote.sh tests/test_tidb_e2e.sh",
    "pkill -f run_tidb_full_sf10_remote.sh || true",
    "pkill -f 'mysql --local-infile' || true",
    "rm -f /opt/datagenx-run/FAILED /opt/datagenx-run/SUCCESS",
    "cat > /opt/datagenx-run/start-full-sf10.sh <<'EOS'\n"
    "#!/usr/bin/env bash\n"
    "set -Eeuo pipefail\n"
    "cd /opt/datagenx-run/work/datagenx_generator\n"
    f"export ARTIFACT_BUCKET='{bucket}'\n"
    f"export ARTIFACT_PREFIX='{artifact_prefix}'\n"
    f"export AWS_REGION='{region}'\n"
    "export BASE_DIR='/opt/datagenx-run'\n"
    "export REPO_DIR='/opt/datagenx-run/work/datagenx_generator'\n"
    f"export RESULTS_DIR='/opt/datagenx-run/results/{artifact_prefix}'\n"
    f"export LOG_DIR='/opt/datagenx-run/logs/{artifact_prefix}'\n"
    "export TIDB_ENV_FILE='/opt/datagenx-run/tidb-cloud.env'\n"
    "export START_LOCAL_TIDB='0'\n"
    "export ENABLE_TIFLASH='1'\n"
    "export TIFLASH_REPLICA_COUNT='3'\n"
    f"export SCHEMA_NAMESPACE='{schema_namespace}'\n"
    f"export REUSE_SOURCE_FILES='{reuse_source_files}'\n"
    f"export REUSE_LOADED_SOURCE_SCHEMA='{reuse_loaded_source_schema}'\n"
    f"export DATAGENX_APPLY_BENCHMARK_FK_DDL='{apply_benchmark_fk_ddl}'\n"
    f"export TPCH_SCALE_FACTOR='{tpch_sf}'\n"
    f"export TPCDS_SCALE_FACTOR='{tpcds_sf}'\n"
    f"exec tests/cloudformation/run_tidb_full_sf10_remote.sh '{profile}'\n"
    "EOS",
    "chmod +x /opt/datagenx-run/start-full-sf10.sh",
    "nohup /opt/datagenx-run/start-full-sf10.sh > /opt/datagenx-run/logs/driver_stdout.log 2>&1 &",
    "echo $! > /opt/datagenx-run/RUN_PID",
    "echo started pid=$(cat /opt/datagenx-run/RUN_PID)",
]
Path = __import__("pathlib").Path
Path(path).write_text(json.dumps({
    "commands": commands,
    "executionTimeout": ["3600"],
}))
PY

    echo "Starting remote run via SSM on $instance_id" >&2
    aws ssm send-command \
        --region "$AWS_REGION" \
        --instance-ids "$instance_id" \
        --document-name "AWS-RunShellScript" \
        --comment "Start DataGenX TiDB full SF10 verification" \
        --parameters "file://$SSM_PARAMS" \
        --query "Command.CommandId" \
        --output text
}

main() {
    require_aws_identity
    deploy_stack
    package_repo

    local instance_id bucket public_ip
    instance_id="$(stack_output InstanceId)"
    bucket="$(stack_output ArtifactBucket)"
    public_ip="$(stack_output PublicIp)"

    wait_for_ssm "$instance_id"
    upload_tidb_env
    upload_repo "$bucket"
    local command_id
    command_id="$(start_remote_run "$instance_id" "$bucket" "$TIDB_ENV_PARAM_NAME")"

    cat <<EOF

Started DataGenX TiDB full SF run.

Stack:          $STACK_NAME
Region:         $AWS_REGION
InstanceId:     $instance_id
PublicIp:       $public_ip
ArtifactBucket: $bucket
TiDBEnvParam:   $TIDB_ENV_PARAM_NAME
SSMCommandId:   $command_id
RunProfile:     $RUN_PROFILE
ArtifactPrefix: $ARTIFACT_PREFIX
SchemaNamespace: ${SCHEMA_NAMESPACE:-<default>}

Check remote bootstrap command:
  aws ssm get-command-invocation --region $AWS_REGION --command-id $command_id --instance-id $instance_id

Tail the long-running job through SSM:
  aws ssm send-command --region $AWS_REGION --instance-ids $instance_id --document-name AWS-RunShellScript --parameters 'commands=["tail -n 200 /opt/datagenx-run/logs/driver_stdout.log"]'

Download synced results after completion:
  aws s3 sync s3://$bucket/$ARTIFACT_PREFIX/results/ results/$ARTIFACT_PREFIX/

The stack is intentionally left running. Delete it only when you are done:
  aws cloudformation delete-stack --region $AWS_REGION --stack-name $STACK_NAME
EOF
}

main "$@"
