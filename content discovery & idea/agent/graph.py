from langgraph.graph import StateGraph, END
from agent.nodes.research import research_node
from agent.nodes.select import select_node
from agent.nodes.generate import generate_node
from agent.nodes.approval import approval_node
from agent.state import AgentState

MAX_RETRIES = 3


def should_retry(state: AgentState) -> str:
    # only a rejection (with feedback) drives another research pass; approved,
    # expired/no-reply, or too many rejections all end the run
    if state["approval_status"] != "rejected":
        return "end"
    if state.get("retry_count", 0) >= MAX_RETRIES:
        return "end"          # give up after repeated rejections
    return "research"         # re-research using the rejection feedback


graph = StateGraph(AgentState)

graph.add_node("research", research_node)
graph.add_node("select", select_node)
graph.add_node("generate", generate_node)
graph.add_node("approval", approval_node)

graph.set_entry_point("research")
graph.add_edge("research", "select")
graph.add_edge("select", "generate")
graph.add_edge("generate", "approval")
graph.add_conditional_edges("approval", should_retry, {
    "research": "research",
    "end": END,
})
