# MBPP implementation supplied as the unit under evaluation.

import heapq as hq
def heap_queue_largest(nums,n):
  largest_nums = hq.nlargest(n, nums)
  return largest_nums