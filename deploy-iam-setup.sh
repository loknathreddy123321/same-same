#!/bin/bash
# One-time IAM setup for the EC2 deployment of cfn-drift-fixer.
# Run this in Git Bash with the aws cli configured for the target account.
set -e

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-south-1

cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
  ]
}
EOF

cat > app-policy.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CfnDrift",
      "Effect": "Allow",
      "Action": [
        "cloudformation:ListStacks",
        "cloudformation:DetectStackDrift",
        "cloudformation:DetectStackResourceDrift",
        "cloudformation:DescribeStackDriftDetectionStatus",
        "cloudformation:DescribeStackResourceDrifts",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:GetTemplate",
        "cloudformation:CreateChangeSet",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:DescribeChangeSet",
        "cloudformation:UpdateStack"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DynamoAudit",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/cfn-drift-audit",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/cfn-drift-approvals"
      ]
    },
    {
      "Sid": "S3Snapshots",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::cfn-drift-snapshots-${ACCOUNT_ID}-dev/*"
    },
    {
      "Sid": "SafetyGateSsm",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter"],
      "Resource": "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/cfn-drift-fixer/*"
    },
    {
      "Sid": "BedrockOptional",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "*"
    },
    {
      "Sid": "EcrPull",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam create-role --role-name cfn-drift-fixer-ec2-role \
  --assume-role-policy-document file://trust-policy.json \
  --description "CFN Drift Fixer dashboard/agent running on EC2" --region "$REGION"

aws iam put-role-policy --role-name cfn-drift-fixer-ec2-role \
  --policy-name cfn-drift-fixer-app-policy \
  --policy-document file://app-policy.json

aws iam attach-role-policy --role-name cfn-drift-fixer-ec2-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam create-instance-profile --instance-profile-name cfn-drift-fixer-ec2-profile

aws iam add-role-to-instance-profile \
  --instance-profile-name cfn-drift-fixer-ec2-profile \
  --role-name cfn-drift-fixer-ec2-role

rm -f trust-policy.json app-policy.json

echo ""
echo "Done. Waiting 10s for IAM propagation..."
sleep 10
aws iam get-instance-profile --instance-profile-name cfn-drift-fixer-ec2-profile --query 'InstanceProfile.Arn' --output text
