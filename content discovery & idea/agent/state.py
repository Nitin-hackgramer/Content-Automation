from typing import TypedDict, Optional

class AgentState(TypedDict):
    target: str                         # "x" | "li" | "both" — which platform(s) to draft (set by the cron/webhook trigger)
    research_data: list[dict]           # sourced items: {title, url, source}
    selected_item: Optional[dict]       # best item after dedup+rank (select_node)
    angle: Optional[str]                # one-line hook/why-it-matters from the ranker
    selected_draft: Optional[str]       # the tweet draft sent for approval
    linkedin_draft: Optional[str]       # long-form LinkedIn post from the same item
    selected_image: Optional[str]       # og:image of the sourced item, if any
    approval_status: str                # "pending" | "approved" | "rejected" | "expired"
    rejection_feedback: Optional[str]   # feedback if rejected (drives next research)
    retry_count: int                    # number of rejections so far
