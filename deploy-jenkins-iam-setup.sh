#!/bin/bash
# Creates a scoped IAM user for Jenkins to push to ECR and deploy via SSM.
# Run this in Git Bash with the aws cli configured for the target account.
set -e

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-south-1
USER_NAME=cfn-drift-fixer-jenkins

cat > jenkins-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrPush",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:BatchGetImage"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SsmDeploy",
      "Effect": "Allow",
      "Action": ["ssm:SendCommand", "ssm:GetCommandInvocation"],
      "Resource": "*"
    },
    {
      "Sid": "Ec2Lookup",
      "Effect": "Allow",
      "Action": ["ec2:DescribeInstances"],
      "Resource": "*"
    },
    {
      "Sid": "S3Source",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::cfn-drift-snapshots-${ACCOUNT_ID}-dev/*"
    }
  ]
}
EOF

aws iam create-user --user-name "$USER_NAME" --region "$REGION" 2>&1 || echo "(user may already exist, continuing)"

aws iam put-user-policy --user-name "$USER_NAME" \
  --policy-name cfn-drift-fixer-jenkins-policy \
  --policy-document file://jenkins-policy.json

rm -f jenkins-policy.json

echo ""
echo "Creating access key (SAVE THIS OUTPUT — the secret is shown only once):"
aws iam create-access-key --user-name "$USER_NAME" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text
