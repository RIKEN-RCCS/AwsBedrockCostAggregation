#!/usr/bin/env python3
"""
APIキーIAMユーザーからタグ経由で所有者名を解決する。

事前準備:
  各 BedrockAPIKey-* IAMユーザーに以下のタグを付ける
    Owner: メンバー識別子
    Email: 連絡先（任意）

使い方:
  from resolve_owner import build_owner_map
  owner_map = build_owner_map()
"""

import boto3


def build_owner_map() -> dict:
    """全 BedrockAPIKey-* ユーザーのタグから所有者マッピングを構築"""
    iam = boto3.client("iam")
    owner_map = {}

    paginator = iam.get_paginator("list_users")
    for page in paginator.paginate():
        for user in page["Users"]:
            user_name = user["UserName"]
            if not user_name.startswith("BedrockAPIKey-"):
                continue

            try:
                tags_response = iam.list_user_tags(UserName=user_name)
                tags = {t["Key"]: t["Value"] for t in tags_response["Tags"]}
            except iam.exceptions.NoSuchEntityException:
                tags = {}

            owner_map[user_name] = {
                "owner": tags.get("Owner", "(未タグ)"),
                "email": tags.get("Email", ""),
                "department": tags.get("Department", ""),
                "project": tags.get("Project", ""),
            }

    return owner_map


def resolve_username(arn: str, owner_map: dict):
    """ARNから (display_name, auth_type) を返す"""
    if not arn:
        return ("unknown", "Unknown")

    if ":user/" in arn:
        user_name = arn.split(":user/")[-1]
        if user_name.startswith("BedrockAPIKey-"):
            info = owner_map.get(user_name, {})
            owner = info.get("owner", "(unknown)")
            return (f"{owner} [{user_name}]", "API Key")
        return (user_name, "IAM User")

    if ":assumed-role/" in arn:
        parts = arn.split(":assumed-role/")[-1]
        return (parts, "Assumed Role")

    return (arn, "Unknown")


if __name__ == "__main__":
    print("=== タグから所有者マッピングを取得 ===\n")
    owner_map = build_owner_map()

    if not owner_map:
        print("BedrockAPIKey-* ユーザーが見つかりませんでした")
    else:
        for api_user, info in owner_map.items():
            print(f"{api_user}")
            print(f"  Owner: {info['owner']}")
            print(f"  Email: {info['email']}")
            print(f"  Project: {info['project']}")
            print()

    print("=== ARN解決テスト ===\n")
    test_arns = [
        "arn:aws:iam::123456789012:user/BedrockAPIKey-sample",
        "arn:aws:iam::123456789012:user/sample-user",
    ]
    for arn in test_arns:
        name, auth = resolve_username(arn, owner_map)
        print(f"{arn}\n  → {name} ({auth})\n")
