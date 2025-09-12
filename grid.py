def build_grid(lower_price: float, upper_price: float,
               grid_count: int | None = None,
               grid_step: float | None = None) -> list[float]:
    """构建升序网格价位（包含上下边界）。

    - 传入 `grid_count`：将区间等分为 `grid_count` 段，返回包含两端在内的价位点。
      例如 lower=0, upper=12, grid_count=3 ⇒ [0, 4, 8, 12]
    - 传入 `grid_step`：从下边界起按步长累加，直到并包含上边界（如能整除）。
    """
    if upper_price <= lower_price:
        raise ValueError("upper_price must be greater than lower_price")
    levels: list[float] = []
    if grid_count and grid_count > 0:
        # 等分为 grid_count 段 ⇒ 返回 grid_count+1 个点（含首尾）。
        # 修正 off-by-one：i 取 [0, grid_count] 共 grid_count+1 个点。
        step = (upper_price - lower_price) / float(grid_count)
        levels = [round(lower_price + step * i, 12) for i in range(int(grid_count) + 1)]
        # 由于浮点误差，强制首尾为边界（避免 0.85 出现重复或最后一点漂移）
        levels[0] = float(lower_price)
        levels[-1] = float(upper_price)
    elif grid_step and grid_step > 0:
        p = float(lower_price)
        levels.append(round(p, 12))
        while p + grid_step < upper_price:
            p += grid_step
            levels.append(round(p, 12))
        # 如果可以整除，补上精确上边界，否则追加上边界确保包含
        if abs((upper_price - lower_price) % grid_step) < 1e-12 or abs(levels[-1] - upper_price) > 1e-12:
            levels.append(float(upper_price))
    else:
        raise ValueError("Either grid_count (>0) or grid_step (>0) must be provided")
    return sorted(levels)
