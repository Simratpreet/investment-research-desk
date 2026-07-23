# ASML — High Na Vs Low Na


### The Core Value Prop: 1 High-NA exposure replaces 3-4 Low-NA exposures

> *"A single High-NA exposure can replace complex multi-patterning processes that today require **three or four Low-NA exposures**. For some critical layers, High-NA can reduce the number of process steps **by a factor of 10**."*

### Think of it like this:

To print the tiniest features on a chip (say, sub-20nm patterns), Low-NA EUV isn't precise enough in a single pass. So customers use a workaround called **multi-patterning** — they expose the wafer 3-4 times with slightly different masks to build up the pattern. This is:

- **Slow** — 3-4 passes instead of 1
- **Expensive** — each pass needs time, chemicals, masks
- **Fab-space hungry** — multi-patterning equipment takes up cleanroom real estate
- **Yield-risky** — more passes = more chances for defects

**High-NA solves this by having a wider lens (higher "numerical aperture")** that can print finer features in a single shot.

### The customer decision framework:

| Factor | Low-NA | High-NA |
|---|---|---|
| **Tool cost** | ~€210-250M | ~€350M+ (1.5-1.7× more) |
| **Feature resolution** | Good for current nodes | Better — 18nm lines, <28nm contacts |
| **Exposures per layer** | 3-4 for critical layers | **1** (single expose) |
| **Fab space needed** | More (multi-patterning tools) | Less (replaces 3-4 tools worth of processing) |
| **Throughput impact** | Lower effective WpH (multiple passes) | Higher effective WpH per layer |
| **Total cost of ownership** | Higher at bleeding-edge nodes | Lower when multi-patterning is eliminated |

### So why isn't everyone buying High-NA today?

1. **Maturity** — it's still being qualified. Only just crossed 500K wafers processed and 80% availability. Low-NA 3800E is battle-tested at >95% availability.
2. **Cost per tool** — at €350M+, it's a huge upfront CapEx hit. Customers need to be sure the tool works before committing.
3. **Limited use cases (for now)** — only the most critical 2-3 layers per chip need the precision High-NA offers. The other ~60-80 layers can still use Low-NA or DUV.
4. **Resist ecosystem** — High-NA needs new photoresist materials that are still maturing.

### DRAM is the surprise early adopter

Christophe specifically flagged that **DRAM has a lower bar** for High-NA adoption:

> *"Especially when it comes to DRAM, the threshold to start using High-NA on existing product is pretty low."*

This is because DRAM patterns are more repetitive and regular than logic patterns, making them easier to print with a new tool. And memory customers are desperately capacity-constrained right now — if High-NA can free up Low-NA tools for other layers, that's immediate capacity relief.

### The bottom line for investors:

Today High-NA is a **margin drag** (Roger: "the number of tools that can absorb the fixed cost is very critical"). But by 2028-2029, as volumes scale from ~10 to 20-30+ units/year, it becomes a **massive revenue and margin lever** — each tool is 1.5× the ASP of Low-NA and replaces 3-4× the process complexity for the customer. The value capture potential is enormous once volumes hit scale.
