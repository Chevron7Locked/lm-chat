def generate_qr_svg(data, module_size=6):
    """Generate a QR code as an SVG string. Pure stdlib, no dependencies.

    Supports QR Version 1-6, ECC Level L, Byte mode encoding.
    Suitable for TOTP URIs (~80-90 chars).
    """
    data_bytes = data.encode('utf-8') if isinstance(data, str) else data

    # --- QR constants ---
    # (total_codewords, ec_codewords_per_block, num_blocks) for ECC Level L, versions 1-6
    VERSION_INFO = {
        1: (26, 7, 1), 2: (44, 10, 1), 3: (70, 15, 1),
        4: (100, 20, 1), 5: (134, 26, 1), 6: (172, 18, 2),
    }
    # Data capacity in bytes for each version at ECC L
    DATA_CAPACITY = {
        v: total - ec * blocks for v, (total, ec, blocks) in VERSION_INFO.items()
    }

    # Pick smallest version that fits
    version = None
    for v in range(1, 7):
        # Byte mode overhead: 4 (mode) + 8 or 16 (count) bits, then data, then terminator
        count_bits = 8 if v <= 9 else 16
        overhead_bits = 4 + count_bits
        avail_bytes = DATA_CAPACITY[v]
        avail_data = (avail_bytes * 8 - overhead_bits) // 8
        if len(data_bytes) <= avail_data:
            version = v
            break
    if version is None:
        raise ValueError(f"Data too long ({len(data_bytes)} bytes) for QR versions 1-6")

    size = 17 + version * 4  # modules per side
    total_cw, ec_per_block, num_blocks = VERSION_INFO[version]
    data_cw = DATA_CAPACITY[version]

    # --- Byte mode encoding ---
    bits = []
    def add_bits(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    add_bits(0b0100, 4)  # Byte mode indicator
    count_bits = 8 if version <= 9 else 16
    add_bits(len(data_bytes), count_bits)
    for b in data_bytes:
        add_bits(b, 8)
    add_bits(0, min(4, data_cw * 8 - len(bits)))  # Terminator
    while len(bits) % 8 != 0:
        bits.append(0)
    # Pad to fill data codewords
    pad_bytes = [0xEC, 0x11]
    pi = 0
    while len(bits) < data_cw * 8:
        add_bits(pad_bytes[pi % 2], 8)
        pi += 1

    data_codewords = []
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            byte = (byte << 1) | bits[i + j]
        data_codewords.append(byte)

    # --- Reed-Solomon over GF(2^8) ---
    PP = 0x11D  # primitive polynomial x^8 + x^4 + x^3 + x^2 + 1
    gf_exp = [0] * 512
    gf_log = [0] * 256
    x = 1
    for i in range(255):
        gf_exp[i] = x
        gf_log[x] = i
        x <<= 1
        if x >= 256:
            x ^= PP
    for i in range(255, 512):
        gf_exp[i] = gf_exp[i - 255]

    def gf_mul(a, b):
        if a == 0 or b == 0:
            return 0
        return gf_exp[gf_log[a] + gf_log[b]]

    def rs_generator(nsym):
        g = [1]
        for i in range(nsym):
            ng = [0] * (len(g) + 1)
            for j in range(len(g)):
                ng[j] ^= g[j]
                ng[j + 1] ^= gf_mul(g[j], gf_exp[i])
            g = ng
        return g

    def rs_encode(data, nsym):
        gen = rs_generator(nsym)
        res = [0] * (len(data) + nsym)
        for i in range(len(data)):
            res[i] = data[i]
        for i in range(len(data)):
            coef = res[i]
            if coef != 0:
                for j in range(len(gen)):
                    res[i + j] ^= gf_mul(gen[j], coef)
        return res[len(data):]

    # Split data into blocks and compute EC
    block_data = []
    idx = 0
    base_dc = data_cw // num_blocks
    extra = data_cw % num_blocks
    for b in range(num_blocks):
        count = base_dc + (1 if b >= num_blocks - extra and extra else 0)
        block_data.append(data_codewords[idx:idx + count])
        idx += count

    block_ec = [rs_encode(bd, ec_per_block) for bd in block_data]

    # Interleave data codewords, then EC codewords
    final = []
    max_dc = max(len(bd) for bd in block_data)
    for i in range(max_dc):
        for bd in block_data:
            if i < len(bd):
                final.append(bd[i])
    for i in range(ec_per_block):
        for be in block_ec:
            if i < len(be):
                final.append(be[i])

    # --- Module placement ---
    # Initialize grid: None = unset, 0 = white, 1 = black
    grid = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def set_module(r, c, val, reserve=True):
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = val
            if reserve:
                reserved[r][c] = True

    # Finder patterns (3 corners)
    def place_finder(row, col):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if 0 <= rr < size and 0 <= cc < size:
                    if 0 <= r <= 6 and 0 <= c <= 6:
                        if (r in (0, 6) or c in (0, 6) or
                                (2 <= r <= 4 and 2 <= c <= 4)):
                            set_module(rr, cc, 1)
                        else:
                            set_module(rr, cc, 0)
                    else:
                        set_module(rr, cc, 0)  # separator

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # Timing patterns
    for i in range(8, size - 8):
        val = 1 if i % 2 == 0 else 0
        if grid[6][i] is None:
            set_module(6, i, val)
        if grid[i][6] is None:
            set_module(i, 6, val)

    # Dark module
    set_module(size - 8, 8, 1)

    # Alignment patterns (version 2+)
    ALIGN_POS = {
        2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    }
    if version >= 2:
        positions = ALIGN_POS[version]
        for ar in positions:
            for ac in positions:
                # Skip if overlapping finder
                if reserved[ar][ac]:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        if abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0):
                            set_module(ar + dr, ac + dc, 1)
                        else:
                            set_module(ar + dr, ac + dc, 0)

    # Reserve format info areas (will be written after masking)
    for i in range(9):
        if not reserved[8][i]:
            reserved[8][i] = True
            grid[8][i] = 0
        if not reserved[i][8]:
            reserved[i][8] = True
            grid[i][8] = 0
    for i in range(8):
        if not reserved[8][size - 1 - i]:
            reserved[8][size - 1 - i] = True
            grid[8][size - 1 - i] = 0
        if not reserved[size - 1 - i][8]:
            reserved[size - 1 - i][8] = True
            grid[size - 1 - i][8] = 0

    # Place data bits
    def place_data(grid_copy, codewords):
        bit_idx = 0
        all_bits = []
        for cw in codewords:
            for i in range(7, -1, -1):
                all_bits.append((cw >> i) & 1)

        col = size - 1
        going_up = True
        while col >= 0:
            if col == 6:  # skip timing column
                col -= 1
                continue
            rows = range(size - 1, -1, -1) if going_up else range(size)
            for row in rows:
                for dc in (0, -1):
                    c = col + dc
                    if c < 0 or c >= size:
                        continue
                    if reserved[row][c]:
                        continue
                    if bit_idx < len(all_bits):
                        grid_copy[row][c] = all_bits[bit_idx]
                        bit_idx += 1
                    else:
                        grid_copy[row][c] = 0
            col -= 2
            going_up = not going_up

    place_data(grid, final)

    # --- Masking ---
    MASK_FNS = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ]

    def apply_mask(grid_src, mask_fn):
        result = [row[:] for row in grid_src]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and mask_fn(r, c):
                    result[r][c] ^= 1
        return result

    def penalty_score(g):
        score = 0
        # Rule 1: runs of 5+ same-colored modules
        for r in range(size):
            count = 1
            for c in range(1, size):
                if g[r][c] == g[r][c - 1]:
                    count += 1
                else:
                    if count >= 5:
                        score += count - 2
                    count = 1
                if c == size - 1 and count >= 5:
                    score += count - 2
        for c in range(size):
            count = 1
            for r in range(1, size):
                if g[r][c] == g[r - 1][c]:
                    count += 1
                else:
                    if count >= 5:
                        score += count - 2
                    count = 1
                if r == size - 1 and count >= 5:
                    score += count - 2
        # Rule 2: 2x2 blocks of same color
        for r in range(size - 1):
            for c in range(size - 1):
                v = g[r][c]
                if g[r][c + 1] == v and g[r + 1][c] == v and g[r + 1][c + 1] == v:
                    score += 3
        return score

    best_mask = 0
    best_score = float('inf')
    best_grid = None
    for mi in range(8):
        mg = apply_mask(grid, MASK_FNS[mi])
        s = penalty_score(mg)
        if s < best_score:
            best_score = s
            best_mask = mi
            best_grid = mg

    # --- Format information ---
    # ECC Level L = 01, mask pattern 3 bits
    format_data = (0b01 << 3) | best_mask  # 5 bits
    # BCH(15,5) encoding
    remainder = format_data << 10
    gen = 0b10100110111  # generator polynomial
    for i in range(4, -1, -1):
        if remainder & (1 << (i + 10)):
            remainder ^= gen << i
    format_bits = ((format_data << 10) | remainder) ^ 0b101010000010010
    # Place format bits
    # Horizontal: left of finder + right of finder
    FORMAT_H = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5),
                (8, 7), (8, 8), (8, size - 8), (8, size - 7),
                (8, size - 6), (8, size - 5), (8, size - 4),
                (8, size - 3), (8, size - 2)]
    # Vertical: below finder + above finder
    FORMAT_V = [(0, 8), (1, 8), (2, 8), (3, 8), (4, 8), (5, 8),
                (7, 8), (8, 8), (size - 7, 8), (size - 6, 8),
                (size - 5, 8), (size - 4, 8), (size - 3, 8),
                (size - 2, 8), (size - 1, 8)]
    for i in range(15):
        bit = (format_bits >> (14 - i)) & 1
        r, c = FORMAT_H[i]
        best_grid[r][c] = bit
        r, c = FORMAT_V[i]
        best_grid[r][c] = bit

    # --- SVG output ---
    quiet = 4  # quiet zone modules
    total = size + quiet * 2
    px = total * module_size
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {px} {px}" '
             f'width="{px}" height="{px}">',
             f'<rect width="{px}" height="{px}" fill="white"/>']
    for r in range(size):
        for c in range(size):
            if best_grid[r][c] == 1:
                x = (c + quiet) * module_size
                y = (r + quiet) * module_size
                parts.append(f'<rect x="{x}" y="{y}" '
                             f'width="{module_size}" height="{module_size}" fill="black"/>')
    parts.append('</svg>')
    return '\n'.join(parts)
