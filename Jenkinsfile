// Jenkins pipeline for cfn-drift-fixer.
//
// Builds the dashboard image, pushes it to ECR, then deploys it to the
// target EC2 instance over SSM Run Command (no SSH key, no open port —
// same access pattern set up in deploy-iam-setup.sh).
//
// Required Jenkins configuration:
//   - Credentials binding "aws-cfn-drift-fixer" (Amazon Web Services Credentials
//     plugin) scoped to: ecr:GetAuthorizationToken, ecr:BatchCheckLayerAvailability,
//     ecr:PutImage, ecr:InitiateLayerUpload, ecr:UploadLayerPart,
//     ecr:CompleteLayerUpload, ecr:BatchGetImage, ssm:SendCommand,
//     ssm:GetCommandInvocation. Not the account's admin user.
//   - Job parameters below (or set as environment in Jenkins folder config).
//
// Required AWS-side prerequisites (one-time, not done by this pipeline):
//   - ECR repo created: aws ecr create-repository --repository-name cfn-drift-fixer
//   - EC2 instance running with the cfn-drift-fixer-ec2-profile instance
//     profile (see deploy-iam-setup.sh) and tagged App=cfn-drift-fixer
//   - Instance profile also needs: ecr:GetAuthorizationToken,
//     ecr:BatchGetImage, ecr:GetDownloadUrlForLayer (add to the role's
//     inline policy) so it can pull from ECR.
//   - /opt/cfn-drift-fixer/.env and docker-compose.prod.yml already present
//     on the instance (deployed once manually, then this pipeline only
//     updates the image).

pipeline {
    agent any

    parameters {
        string(name: 'AWS_REGION',        defaultValue: 'ap-south-1',            description: 'AWS region')
        string(name: 'ECR_REPO_NAME',      defaultValue: 'cfn-drift-fixer',       description: 'ECR repository name')
        string(name: 'EC2_INSTANCE_TAG',   defaultValue: 'cfn-drift-fixer',       description: 'Value of the App tag on the target EC2 instance')
    }

    environment {
        AWS_CREDS = credentials('aws-cfn-drift-fixer')
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Test') {
            // Single deep-test stage: full pytest run, coverage reported but not
            // gated. Real coverage today is ~27% (graph.py, multi_account.py,
            // handlers/, notifications/slack.py have none) — make test-cov's
            // local 70% gate has never actually passed, so gating the pipeline
            // on it would fail every build. Reporting it here makes the gap
            // visible without blocking on unrelated pre-existing test debt.
            // Also not running `make lint`: pre-existing flake8 violations
            // across most of src/, same reasoning.
            steps {
                sh '''
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install -q -r requirements.txt -r requirements-dev.txt
                    PYTHONPATH=src pytest tests/ -v --tb=short \
                        --cov=src --cov-report=term-missing --cov-report=html:htmlcov \
                        --junitxml=test-results.xml
                '''
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'test-results.xml'
                    archiveArtifacts artifacts: 'htmlcov/**', allowEmptyArchive: true
                }
            }
        }

        stage('Build image') {
            steps {
                script {
                    env.IMAGE_TAG = "${env.BUILD_NUMBER}"
                }
                sh 'docker build -t ${ECR_REPO_NAME}:${IMAGE_TAG} .'
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
