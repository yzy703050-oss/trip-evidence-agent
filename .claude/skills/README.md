# Runtime roles and guides

Only four child agents are registered:

| Guide | Business identity | Responsibility |
| --- | --- | --- |
| preference | preference | Extract persistent preference changes |
| memory-query | memory_query | Retrieve saved history and profile |
| ask-question | rag_knowledge | Existing policy RAG, unchanged |
| query-info | information_query | Collect conditions, call private tools, check and summarize |

MainAgent plans the turn and optionally synthesizes answers or itineraries.
plan-trip is its itinerary guide, with no registered child-agent identity.
The event-collection, train-search, hotel-search and travel-guide documents are
retained references; their former agent execution scripts have been retired.
Information tools live in travel_data/tools.py, with schemas visible only to
information_query. Skill folder names cannot bypass the business registry.
Complete simple queries forward the information answer through one CLI output;
partial results and combined tasks return to MainAgent. RAG internals are frozen.
