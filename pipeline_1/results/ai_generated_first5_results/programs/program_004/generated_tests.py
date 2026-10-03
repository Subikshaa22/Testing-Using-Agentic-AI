# Copilot Chat prompt:
# For the first five MBPP programs, use the coverage objective and cover all branches, including normal, edge, and boundary inputs.
# Tests were generated and executed by the AI agent.

assert heap_queue_largest([3, 1, 2], 0) == []
assert heap_queue_largest([3, 1, 2], 1) == [3]
assert heap_queue_largest([3, 1, 2], 3) == [3, 2, 1]
