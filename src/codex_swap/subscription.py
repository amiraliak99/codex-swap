"""Display plan labels available from Codex's local account claims."""

PLAN_TTL_S = float("inf")


def plan_label(plan: dict) -> str:
    tier = plan["tier"]
    return tier.replace("_", " ").title()


def is_expired(plan: dict) -> bool:
    return plan.get("subscriptionStatus") == "expired"
