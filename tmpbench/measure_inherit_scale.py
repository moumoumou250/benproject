"""How many times is the inherited block paid for across a scan?"""
hist_tokens = 7914          # measured for a 61-item parent history
child_turns = 12            # settings.LlmSettings.max_turns default
children = 4                # typical parallel fan-out

# Case 1: inherited message is turn-1 input only; later turns carry session history,
# so the block is present in every subsequent request's context anyway (context grows).
# It is billed once as input per request that includes it -> children * turns.
print("tokens billed for the inherited block (worst case, replayed each turn):")
print(f"  {children} children x {child_turns} turns x {hist_tokens} = {children*child_turns*hist_tokens:,} tokens")

# Case 2: if it were compacted to 339 chars (~85 tokens)
compact_tokens = 85
print(f"  compact: {children} x {child_turns} x {compact_tokens} = {children*child_turns*compact_tokens:,} tokens")
print(f"  saving: {(children*child_turns*(hist_tokens-compact_tokens)):,} tokens")

# Also: cache miss penalty. The block sits in a user message after <cache_point>,
# so it cannot be cached as prefix; every child restarts the cache cold.
print()
print("prompt-cache note: the block is user-message content after the cache boundary,")
print("so each child's first request is a full-price cache *write*, not a read.")
