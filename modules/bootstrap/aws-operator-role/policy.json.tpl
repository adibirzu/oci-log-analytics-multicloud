{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadIdentityAndListings",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity",
        "s3:ListAllMyBuckets",
        "sqs:ListQueues",
        "cloudtrail:DescribeTrails",
        "cloudtrail:ListTrails",
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3BucketWrite",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:PutBucketNotification",
        "s3:PutLifecycleConfiguration",
        "s3:PutEncryptionConfiguration",
        "s3:PutBucketOwnershipControls",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketTagging"
      ],
      "Resource": "arn:${partition}:s3:::${name_prefix}*"
    },
    {
      "Sid": "S3BucketRead",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:GetAccelerateConfiguration",
        "s3:GetBucketAcl",
        "s3:GetBucketCORS",
        "s3:GetBucketLocation",
        "s3:GetBucketLogging",
        "s3:GetBucketNotification",
        "s3:GetBucketObjectLockConfiguration",
        "s3:GetBucketOwnershipControls",
        "s3:GetBucketPolicy",
        "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketRequestPayment",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetBucketWebsite",
        "s3:GetEncryptionConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:GetReplicationConfiguration"
      ],
      "Resource": "arn:${partition}:s3:::${name_prefix}*"
    },
    {
      "Sid": "S3ObjectReadAndTeardown",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion"
      ],
      "Resource": "arn:${partition}:s3:::${name_prefix}*/*"
    },
    {
      "Sid": "SqsQueueWrite",
      "Effect": "Allow",
      "Action": [
        "sqs:CreateQueue",
        "sqs:DeleteQueue",
        "sqs:SetQueueAttributes",
        "sqs:TagQueue",
        "sqs:UntagQueue"
      ],
      "Resource": "arn:${partition}:sqs:*:${account_id}:${name_prefix}*"
    },
    {
      "Sid": "SqsQueueReadAndCollectorTest",
      "Effect": "Allow",
      "Action": [
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "sqs:ListQueueTags",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility"
      ],
      "Resource": "arn:${partition}:sqs:*:${account_id}:${name_prefix}*"
    },
    {
      "Sid": "CloudTrailWrite",
      "Effect": "Allow",
      "Action": [
        "cloudtrail:CreateTrail",
        "cloudtrail:DeleteTrail",
        "cloudtrail:UpdateTrail",
        "cloudtrail:StartLogging",
        "cloudtrail:StopLogging",
        "cloudtrail:PutEventSelectors",
        "cloudtrail:AddTags",
        "cloudtrail:RemoveTags"
      ],
      "Resource": "arn:${partition}:cloudtrail:*:${account_id}:trail/${name_prefix}*"
    },
    {
      "Sid": "CloudTrailRead",
      "Effect": "Allow",
      "Action": [
        "cloudtrail:GetTrail",
        "cloudtrail:GetTrailStatus",
        "cloudtrail:GetEventSelectors",
        "cloudtrail:GetInsightSelectors",
        "cloudtrail:ListTags"
      ],
      "Resource": "arn:${partition}:cloudtrail:*:${account_id}:trail/${name_prefix}*"
    },
    {
      "Sid": "IamRoleWrite",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "Resource": "arn:${partition}:iam::${account_id}:role/${name_prefix}*"
    },
    {
      "Sid": "IamRoleRead",
      "Effect": "Allow",
      "Action": [
        "iam:GetRole",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:ListInstanceProfilesForRole"
      ],
      "Resource": "arn:${partition}:iam::${account_id}:role/${name_prefix}*"
    },
    {
      "Sid": "IamOidcProviderWrite",
      "Effect": "Allow",
      "Action": [
        "iam:CreateOpenIDConnectProvider",
        "iam:DeleteOpenIDConnectProvider",
        "iam:GetOpenIDConnectProvider",
        "iam:TagOpenIDConnectProvider",
        "iam:UntagOpenIDConnectProvider",
        "iam:UpdateOpenIDConnectProviderThumbprint",
        "iam:AddClientIDToOpenIDConnectProvider",
        "iam:RemoveClientIDFromOpenIDConnectProvider"
      ],
      "Resource": ${oidc_provider_resources}
    },
    {
      "Sid": "AssumeCollectorRoleForTestRun",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:${partition}:iam::${account_id}:role/${name_prefix}*collector"
    },
    {
      "Sid": "DenyIamWritesOutsidePrefix",
      "Effect": "Deny",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "NotResource": "arn:${partition}:iam::${account_id}:role/${name_prefix}*"
    },
    {
      "Sid": "DenyManagedPolicyAttachmentAndBoundaryChanges",
      "Effect": "Deny",
      "Action": [
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:AttachUserPolicy",
        "iam:AttachGroupPolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary",
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:SetDefaultPolicyVersion"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyOperatorRoleSelfModification",
      "Effect": "Deny",
      "Action": [
        "iam:DeleteRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "Resource": "arn:${partition}:iam::${account_id}:role/${operator_role_name}"
    }
  ]
}
