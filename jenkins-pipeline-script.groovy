// Pipeline for the "AI angent" Jenkins job.
// Source comes from S3 (not GitHub yet) - see SOURCE_S3_URI parameter.
// Requires a Jenkins credentials entry named "aws-cfn-drift-fixer"
// (AWS credentials, scoped to ECR push + ssm:SendCommand/GetCommandInvocation
// + s3:GetObject on the source package - not the account admin user).
// NOTE: keep this file plain-ASCII. An em dash here once broke the config.xml
// push to Jenkins with an opaque 500 (UTF-8 mangled somewhere in the
// Windows-python -> Git Bash -> curl chain) even though the XML itself
// was valid UTF-8 and well-formed.
pipeline {
    agent any

    parameters {
        string(name: 'AWS_REGION',        defaultValue: 'ap-south-1', description: 'AWS region')
        string(name: 'ECR_REPO_NAME',     defaultValue: 'cfn-drift-fixer', description: 'ECR repository name')
        string(name: 'EC2_INSTANCE_TAG',  defaultValue: 'cfn-drift-fixer', description: 'Value of the App tag on the target EC2 instance')
        string(name: 'SOURCE_S3_URI',     defaultValue: 's3://cfn-drift-snapshots-801945369072-dev/deploy/cfn-drift-fixer-latest.tar.gz', description: 'S3 location of the source package')
    }

    environment {
        AWS_CREDS = credentials('aws-cfn-drift-fixer')
    }

    stages {
        stage('Fetch source') {
            steps {
                withEnv(["AWS_ACCESS_KEY_ID=${AWS_CREDS_USR}", "AWS_SECRET_ACCESS_KEY=${AWS_CREDS_PSW}", "AWS_DEFAULT_REGION=${AWS_REGION}"]) {
                    sh '''
                        rm -rf src_pkg && mkdir src_pkg
                        aws s3 cp "${SOURCE_S3_URI}" /tmp/src.tar.gz
                        tar xzf /tmp/src.tar.gz -C src_pkg
                    '''
                }
            }
        }

        stage('Test') {
            // Coverage reported, not gated — real coverage is ~27% today
            // (pre-existing gap unrelated to this pipeline).
            steps {
                dir('src_pkg') {
                    sh '''
                        python3 -m venv .venv
                        . .venv/bin/activate
                        pip install -q -r requirements.txt -r requirements-dev.txt
                        PYTHONPATH=src pytest tests/ -v --tb=short \
                            --cov=src --cov-report=term-missing --cov-report=html:htmlcov \
                            --junitxml=test-results.xml
                    '''
                }
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'src_pkg/test-results.xml'
                    archiveArtifacts artifacts: 'src_pkg/htmlcov/**', allowEmptyArchive: true
                }
            }
        }

        stage('Build image') {
            steps {
                script { env.IMAGE_TAG = "${env.BUILD_NUMBER}" }
                dir('src_pkg') {
                    sh 'docker build -t ${ECR_REPO_NAME}:${IMAGE_TAG} .'
                }
            }
        }

        stage('Push to ECR') {
            steps {
                withEnv(["AWS_ACCESS_KEY_ID=${AWS_CREDS_USR}", "AWS_SECRET_ACCESS_KEY=${AWS_CREDS_PSW}", "AWS_DEFAULT_REGION=${AWS_REGION}"]) {
                    sh '''
                        ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
                        ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

                        aws ecr get-login-password --region "${AWS_REGION}" \
                          | docker login --username AWS --password-stdin "${ECR_URI}"

                        docker tag ${ECR_REPO_NAME}:${IMAGE_TAG} "${ECR_URI}:${IMAGE_TAG}"
                        docker tag ${ECR_REPO_NAME}:${IMAGE_TAG} "${ECR_URI}:latest"
                        docker push "${ECR_URI}:${IMAGE_TAG}"
                        docker push "${ECR_URI}:latest"

                        echo "${ECR_URI}:${IMAGE_TAG}" > ecr_image.txt
                    '''
                }
            }
        }

        stage('Deploy to EC2 via SSM') {
            steps {
                withEnv(["AWS_ACCESS_KEY_ID=${AWS_CREDS_USR}", "AWS_SECRET_ACCESS_KEY=${AWS_CREDS_PSW}", "AWS_DEFAULT_REGION=${AWS_REGION}"]) {
                    sh '''
                        ECR_IMAGE=$(cat ecr_image.txt)
                        ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
                        ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

                        INSTANCE_ID=$(aws ec2 describe-instances \
                          --filters "Name=tag:App,Values=${EC2_INSTANCE_TAG}" "Name=instance-state-name,Values=running" \
                          --query 'Reservations[0].Instances[0].InstanceId' --output text --region "${AWS_REGION}")

                        if [ "$INSTANCE_ID" = "None" ] || [ -z "$INSTANCE_ID" ]; then
                          echo "No running instance tagged App=${EC2_INSTANCE_TAG} found."
                          exit 1
                        fi

                        COMMAND_ID=$(aws ssm send-command \
                          --instance-ids "$INSTANCE_ID" \
                          --document-name "AWS-RunShellScript" \
                          --parameters "commands=[
                            \\"cd /opt/cfn-drift-fixer\\",
                            \\"aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_URI}\\",
                            \\"ECR_IMAGE=${ECR_IMAGE} docker compose -f docker-compose.prod.yml pull\\",
                            \\"ECR_IMAGE=${ECR_IMAGE} docker compose -f docker-compose.prod.yml up -d\\",
                            \\"docker image prune -f\\"
                          ]" \
                          --region "${AWS_REGION}" \
                          --query 'Command.CommandId' --output text)

                        aws ssm wait command-executed --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" --region "${AWS_REGION}" || true
                        aws ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE_ID" --region "${AWS_REGION}"
                    '''
                }
            }
        }
    }

    post {
        always {
            sh 'docker logout || true'
        }
    }
}
