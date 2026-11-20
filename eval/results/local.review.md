# Review: local

For each question, replace `?` after **Grade:** with `correct`, `partial` or `incorrect`.
For questions the papers do not answer, `correct` means the system said so.

## q01: Why does Bipartite Modular Multiplication give the correct result?

**Expected:** In the KT-residue system the multiplier is split as $Y = Y_H r^{\alpha n} + Y_L$, and $X \circledast Y = (X Y_H \bmod M + X Y_L r^{-\alpha n} \bmod M) \bmod M$, which equals $X Y r^{-\alpha n} \bmod M$. The interleaved algorithm computes the $Y_H$ term and the Montgomery algorithm the $Y_L$ term, in parallel.

**Answer:**

Bipartite Modular Multiplication gives the correct result because it operates within a new residue class representation modulo $M$ where the transformation constants satisfy specific mathematical properties, and the final step correctly combines the parallel partial products.

The method defines a new residue system using a constant $R = r^{\alpha n}$ (where $r=2^k$, $\gcd(M,r)=1$, and $0<\alpha<1$). The image of an integer $U$ is $X = U \cdot R \bmod M$. Multiplication in this new system is defined as:
$$ X \circledast Y \triangleq X \cdot Y \cdot r^{-\alpha n} \bmod M $$
The existence of the inverse term $r^{-\alpha n} \bmod M$ is assured because $R = r^{\alpha n}$ is relatively prime to $M$.

According to **Algorithm 5.3** [4], the algorithm splits the multiplier $Y$ into two parts, $Y_H$ and $Y_L$, such that $Y = Y_H \cdot r^{\alpha n} + Y_L$. It then computes two partial products in parallel:
1.  $S = \text{Interleaved\_modmul}(W, A, M)$, where $A = Y_H$.
2.  $T = \text{Montgomery\_modmul}(W, B, M)$, where $B = Y_L$.

The final result is obtained by summing these partial products and applying the inverse transformation:
$$ Z = (S + T) \bmod M $$

As stated in **Section 5.3** [3], this process ensures that the multiplication modulo $M$ over the images of $U$ and $V$ results in an image of $U \cdot V \bmod M$. The correctness relies on the fact that the split calculation is independent, allowing the use of different algorithms (interleaved and Montgomery) for each part, which are then combined via modular addition to yield the correct final product.

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Pipelined Bipartite Modular Multiplication
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Abstract
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Concluding Remarks

**Grade:** partial

## q02: What is the difference between modular division based on the extended binary GCD algorithm and modular division based on the extended Euclidean algorithm?

**Expected:** The extended Euclidean algorithm reduces A and B through integer divisions (quotient q, A - qB); the extended binary GCD algorithm uses only halving of even values and subtraction of odd ones. Both apply the same operations to U and V modulo M. In the thesis the binary GCD divider is a radix-4/2 design (MQRTR) combined with Montgomery multiplication; the Euclidean one improves Takagi's divider by sharing the modular reduction hardware and also computes ordinary modular multiplication.

**Answer:**

Based on the provided sources, the primary differences between the two algorithms lie in their iterative properties, variable requirements, and specific reduction rules.

**1. Iterative Properties and Reduction Rules**
*   **Extended Binary GCD:** Uses properties related to division by 2 and subtraction. When $A$ is even and $B$ is odd, $\gcd(A,B)=\gcd(A/2,B)$. When both are odd and $A \ge B$, it uses $\gcd(A,B)=\gcd(A-B,B)$ [1].
*   **Extended Euclidean:** Uses the property of integer division. It calculates the GCD through a series of integer divisions using the rule $\gcd(A,B)=\gcd(A-\lfloor \frac{A}{B} \rfloor \cdot B, B)$ when $A \ge B$ [2].

**2. Variable Requirements**
*   **Extended Binary GCD:** Requires four variables: $A$, $B$, $U$, and $V$. Variables $A$ and $B$ are used for $\gcd(Y,M)$, while $U$ and $V$ track the modular quotient [1][5].
*   **Extended Euclidean:** The source describes the GCD algorithm using variables $A$ and $B$ initialized to $X$ and $Y$, storing the remainder $A'$ in a loop. For the extended version for modular division, it implies similar variable usage but emphasizes the use of integer division steps involving a quotient digit $q = \lfloor A/B \rfloor$ [2].

**3. Hardware Implementation Characteristics**
*   **Extended Binary GCD:** The hardware algorithm based on this method processes operands and is noted to share resources with Montgomery multiplication by processing from the most significant digit first (though the text in [5] suggests a modification for acceleration involving sums/differences of odd numbers). It employs binary signed-digit (SD2) representation to avoid carry propagation [3][5].
*   **Extended Euclidean:** The hardware algorithm based on this method also processes operands from the most significant position. It shares resources with ordinary modular multiplication and Montgomery multiplication by combining similar operations. It modifies the original Takagi algorithm to reduce hardware requirements, specifically by strongly normalizing variable $B$ (e.g., ensuring $b_{n-1} \neq 0$ and $b_{n-2}=0$) to simplify quotient digit determination via subtractions in the SD2 system [3][4][6].

In summary, the **Extended Binary GCD** relies on halving even numbers and subtracting odd numbers, requiring four specific variables for quotient tracking. The **Extended Euclidean** relies on repeated integer division steps (calculating $q = \lfloor A/B \rfloor$) and involves hardware optimizations such as strong normalization of the divisor variable to facilitate subtraction-based division in SD2 representation [1][2].

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Preliminaries > Modular Division > Extended Binary GCD Algorithm for Modular Division
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Preliminaries > Modular Division > Extended Euclidean Algorithm for Modular Division
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Introduction
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Binary GCD Algorithm > Similarity Between the Extended Binary GCD Algorithm and the Montgomery Multiplication Algorithm
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Hardware Algorithms for Modular Multiplication and Division Based on the Extended Eucliden Algorithm > An Improved Hardware Algorithm for Modular Division Based on the Extended Euclidean Algorithm

**Grade:** partial

## q03: What is the difference between the Montgomery multiplication algorithm and the interleaved modular multiplication algorithm?

**Expected:** Interleaved modular multiplication computes $X Y \bmod M$, processing the multiplier from the most significant digit and estimating the quotient $\lfloor Z/M \rfloor$. Montgomery multiplication works on M-residues of an odd modulus, computes $X Y R^{-1} \bmod M$ and processes the multiplier from the least significant digit, choosing a multiple of M that makes Z divisible by r, with no carry propagation in the quotient. The two are symmetric.

**Answer:**

Based on the provided sources, the differences between the **Montgomery multiplication algorithm** and the **interleaved modular multiplication algorithm** are as follows:

1.  **Processing Order of the Multiplier ($Y$):**
    *   In the **interleaved modular multiplication algorithm**, the multiplier $Y$ is processed from the **most significant position**. [1]
    *   In the **Montgomery multiplication algorithm**, the multiplier is processed from the **least significant position**. [1][4]

2.  **Operation on Intermediate Product ($Z$):**
    *   In the **interleaved modular multiplication algorithm**, the intermediate accumulated product $Z$ is multiplied by $r$, which can be implemented as a **left-shift operation**. [1]
    *   In the **Montgomery multiplication algorithm**, the partial product $Z$ is divided by $r$, which can be implemented as a **right-shift operation**. [1]

3.  **Determination of Quotient ($q$):**
    *   In the **interleaved modular multiplication algorithm**, the quotient $q_C$ is determined using the **most significant few digits** of $Z$ and $M$. [1]
    *   In the **Montgomery multiplication algorithm**, the quotient $q_M$ is determined using the **least significant digit** of $Z$ and $M$. [1]

4.  **Operand Processing Direction (General):**
    *   The interleaved modular multiplication algorithm processes operands from the **most significant digit position**. [4]
    *   The Montgomery multiplication algorithm processes operands from the **least significant digit position**. [4]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Symmetry Between the Interleaved Modular Multiplication Algorithm and the Montgomery Multiplication Algorithm
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Symmetry Between the Interleaved Modular Multiplication Algorithm and the Montgomery Multiplication Algorithm
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Hardware Implementation
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Binary GCD Algorithm > Similarity Between the Extended Binary GCD Algorithm and the Montgomery Multiplication Algorithm

**Grade:** partial

## q04: How does addition work in the binary signed-digit (SD2) representation?

**Expected:** Digits are in $\{\bar 1, 0, 1\}$. For each position an interim sum $h_i$ and a carry $c_i$ are computed from $a_i, b_i$ and the preceding digits (Table 2.1), then $s_i = h_i + c_{i-1}$. There is no carry propagation and all digits are computed in parallel.

**Answer:**

In the binary signed-digit (SD2) representation with digit set $\{\bar1, 0, 1\}$ (where $\bar1$ denotes $-1$), addition of two SD2 numbers $A$ and $B$ is performed without carry propagation. The process involves calculating an interim sum $h_i$ and a carry digit $c_i$, followed by a final sum $s_i$.

The calculation for each bit position $i$ proceeds as follows:
1.  Calculate the interim sum: $h_i = a_i + b_i + c_i$.
2.  Calculate the carry digit: $c_i = \lfloor h_i / 2 \rfloor$.
3.  Calculate the final sum digit: $s_i = h_i + c_{i-1}$.

All digits of the result can be computed in parallel using these rules [1]. The specific values for $a_i, b_i$ and their preceding bits ($a_{i-1}, b_{i-1}, a_{i-2}, b_{i-2}$) are required to determine $s_i$, with the exact computation details provided in Table 2.1 of the source [1].

To perform subtraction, the additive inverse of one operand is found by changing the signs of all nonzero digits, followed by an addition operation [1].

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Preliminaries > Binary Signed-Digit Number Representation
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Preliminaries > Basic Operations in SD2 System
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > A Radix-$4$ Implementation Example
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Hardware Algorithms for Modular Multiplication and Division Based on the Extended Eucliden Algorithm > An Improved Hardware Algorithm for Modular Division Based on the Extended Euclidean Algorithm
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Binary GCD Algorithm > Hardware Algorithms for Modular Multiplication and Division Based on the Extended Binary GCD Algorithm > A Hardware Algorithm for Montgomery Modular Multiplication
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > A Radix-$4$ Implementation Example

**Grade:** incorrect

## q05: Can the multiplier be split into more than two parts in Bipartite Modular Multiplication?

**Expected:** Not covered: the thesis only describes splitting the multiplier into two parts, $Y_H$ and $Y_L$ (possibly of unequal length via $\alpha$). A good answer says the sources do not discuss more parts.

**Answer:**

Based on the provided sources, the Bipartite Modular Multiplication method specifically splits the multiplier into **two** parts ($Y_H$ and $Y_L$) to process them in parallel using different algorithms (interleaved modular multiplication and Montgomery multiplication). The text does not mention or support splitting the multiplier into more than two parts.

The method relies on an isomorphism between the original integer set and a residue system, where the multiplier $Y$ is defined as $Y = Y_H \cdot r^{\alpha n} + Y_L$ [3]. While the parameter $\alpha$ allows for unequal splitting of the two parts to match different hardware performance levels (e.g., radix-2 vs. radix-4 multipliers) [5], the fundamental architecture described processes exactly two split operands simultaneously [6].

Therefore, according to the provided sources, the multiplier is split into **two** parts, not more.

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Introduction
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Pipelined Bipartite Modular Multiplication
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Hardware Implementation
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method

**Grade:** correct

## q06: How many PlayStation 3 consoles did LACAL use to solve the 112-bit prime field ECDLP?

**Expected:** More than 215 PS3s (about 1300 SPUs). It took about half a year and was completed on July 8, 2009.

**Answer:**

Based on the provided sources, **LACAL** (or the authors' team) used **6** PlayStation 3 consoles (specifically using 6 SPUs per console) to solve the discrete logarithm problem for the 112-bit prime field elliptic curve.

Source [2] states: "For the special 112-bit curve we found that the overall calculation can be expected to take approximately **60 PS3 years**[^4]. Since we use just **6 SPUs**, better performance can be obtained if we would use the PPU too, or if we would be able to access the $7^{\text{th}}$ SPU."

Source [2] further clarifies the extrapolation for 160-bit ECC: "Since we use just 6 SPUs... Extrapolating while including a 20% generic slowdown... we find $\approx 2.5 \cdot 10^9$ PS3 years to crack 160-bit ECC." This implies the base unit of calculation was derived from the 6-SPU configuration on the PS3s.

Source [4] mentions: "2.5 billion current workstations should be able to crack 160-bit ECC within a time-frame of 6 to 24 months". Given that 6 SPUs are equivalent to roughly one quad-core processor (as stated in Source [2]: "a year on 2.5 billion devices suffices" where the comparison is made against desktops), the specific cluster run for the 112-bit case utilized **6** processing units across the available PS3 consoles.

Answer: **6** SPUs (Source [2]).

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Discrete logarithms
- [6] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups

**Grade:** incorrect

## q07: What is the birthday paradox, and why does it matter for Pollard's rho method?

**Expected:** 23 randomly picked people give a better than 50% chance that two share a birthday. Drawing with replacement from N objects, a repeat is expected after about $\sqrt{\pi N/2}$ draws, so Pollard's rho expects a collision after about $\sqrt{\pi q/2}$ steps.

**Answer:**

The **birthday paradox** states that if random objects are selected with replacement from $N$ objects, one may expect $\sqrt{\pi N/2}$ rounds before an object is picked twice [1]. For example, picking 23 people at random yields a more than 50% chance that two share the same birthday [1].

It matters for **Pollard's rho** because the method relies on finding duplicate points in a group $G$ of order $q$. Due to the birthday paradox, after approximately $\sqrt{\pi q/2}$ pairs (or steps) are examined, a collision is expected: two pairs $(r,s)$ and $(u,v)$ such that $g^r y^s = g^u y^v$ [3]. If $v \not\equiv s \pmod q$, this leads to the solution $x = \frac{r-u}{v-s} \bmod q$ [3].

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > The birthday paradox
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Pollard’s rho method to compute discrete logarithms
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [6] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Parallelized Pollard rho with distinguished points

**Grade:** correct

## q08: How does Pollard's rho method compute discrete logarithms?

**Expected:** It looks at $g^r y^s$ for random pairs $(r, s)$ (in practice a pseudo-random walk). By the birthday paradox a duplicate $g^r y^s = g^u y^v$ appears after about $\sqrt{\pi q/2}$ steps, giving $x = (r-u)/(v-s) \bmod q$. It parallelises with distinguished points.

**Answer:**

Pollard's rho method computes the discrete logarithm $x$ of an element $y$ with respect to a generator $g$ (where $g^x = y$) by searching for duplicate points in a sequence generated by a random walk. The method considers elements of the form $g^r y^s$ for random pairs of integers $(r, s)$ [1].

The algorithm proceeds as follows:
1.  **Partitioning**: The group $G$ is partitioned into $t$ parts ($G_1, \ldots, G_t$) to allow quick determination of which part an element belongs to [1].
2.  **Random Walk Initialization**: A start point $w_0$ (a random power of $g$) is chosen. For each step $j$, a random pair $(r_i, s_i)$ is selected, and the next point $w_{j+1}$ is calculated as $p_i w_j$, where $p_i = g^{r_i} y^{s_i}$ [1].
3.  **Cycle Detection**: To find duplicate points $(w_k, w_{2k})$ such that $w_k = w_{2k}$, Floyd's cycle finding trick is used [1]. On average, a collision occurs at step $k \approx \sqrt{\pi q/2}$, where $q$ is the order of the group [1].
4.  **Logarithm Recovery**: If a duplicate is found for pairs $(r, s)$ and $(u, v)$ such that $g^r y^s = g^u y^v$, then unless $v \equiv s \bmod q$, the discrete logarithm is computed as:
    $$ x = \frac{r-u}{v-s} \bmod q $$
    [1]

This approach allows for full parallelization on loosely coupled processing elements, where each process requires only a small constant number of $(\log_2 q)$-bit numbers of memory [2].

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Pollard’s rho method to compute discrete logarithms
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Discrete logarithms
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.5 Timings
- [6] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups

**Grade:** correct

## q09: Did LACAL solve the 160-bit prime field ECDLP?

**Expected:** No. The paper estimates about $2.5 \cdot 10^9$ PS3 years (a year on 2.5 billion devices) and considers an open-community attack even by 2020 very unlikely. LACAL solved the 112-bit one.

**Answer:**

No, the provided sources do not state that LACAL solved the 160-bit prime field Elliptic Curve Discrete Logarithm Problem (ECDLP).

The sources indicate that solving a 160-bit ECDLP is currently infeasible with available resources:
*   Extrapolating from a 2002 result on a 109-bit curve, the authors estimate it would take approximately **50 billion years** to attack 160-bit ECC on a single core [4].
*   Using PlayStation 3 (PS3) hardware with 6 SPUs, they estimate cracking 160-bit ECC would take approximately **2.5 billion PS3 years** [1].
*   They conclude that "with the current state of the art in cryptanalysis we would be surprised if a public effort can make a dent in 160-bit prime field ECC by the year 2020" [2].

The authors do mention solving an ECDLP on a **131-bit binary extension field** using a cluster of PS3s, but this is distinct from the 160-bit prime field case [3].

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, Front matter
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 4 Conclusion
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA

**Grade:** correct

## q10: Was the 112-bit prime field ECDLP computation supported by Sony?

**Expected:** Not stated: neither paper says Sony supported it. The acknowledged support is the Swiss National Science Foundation, EPFL DIT and the EU ICT program ECRYPT II.

**Answer:**

No, the source text does not state that Sony supported the 112-bit prime field ECDLP computation. It describes the authors' own implementation on PlayStation 3 (PS3) hardware [1]. The text mentions that one of the eight Synergistic Processing Elements (SPEs) is "reserved by Sony's hypervisor" and unavailable to the users running Linux [3], but it does not indicate any official support or sponsorship for the specific cryptographic computations performed.

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 1 Introduction
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, Front matter
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC

**Grade:** correct

## q12: How does cracking 160-bit ECC compare with cracking 1024-bit RSA?

**Expected:** Cracking 160-bit ECC is at least three orders of magnitude harder. 160-bit ECC gives about 86-87 bits of security, 1024-bit RSA about 76 bits.

**Answer:**

Cracking 160-bit ECC is at least three orders of magnitude harder than cracking 1024-bit RSA [1]. Specifically, the computational difficulty gap is a factor of one thousand ($10^3$) by the year 2020 [1].

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 4 Conclusion
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 1 Introduction
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, Front matter
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC

**Grade:** correct

## q13: Can the PlayStation 3's graphics card be used to solve the ECDLP?

**Expected:** Not at the time: with the hypervisor version then current it was not accessible to applications other than games. If it were accessible it could double a PS3's effectiveness.

**Answer:**

No, the provided sources do not state that the PlayStation 3's **graphics card** can be used to solve the Elliptic Curve Discrete Logarithm Problem (ECDLP).

The source [2] explicitly compares the performance of PS3 SPUs against graphics cards for modular multiplications but concludes that graphics cards are less attractive due to price and power consumption:
> "The price and power consumption of graphic cards, however, both look less attractive than those of a PS3." [2]

While source [2] mentions that a GTX 295 graphics card can perform about 42 million modular multiplications per second for 280-bit moduli, it does not confirm its use or capability for solving ECDLP. Source [5] specifies that the ECDLP experiments were conducted using **more than 200 PlayStation 3 game consoles** (implying the SPUs), not graphics cards.

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [4] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [5] Number crunching on clusters at EPFL, Introduction
- [6] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Discrete logarithms

**Grade:** incorrect

## q14: What is the most effective published method for factoring an RSA modulus?

**Expected:** The Number Field Sieve (NFS).

**Answer:**

The most effective published method for factoring an RSA modulus is the **Number Field Sieve** (NFS) [1][4].

While the Special Number Field Sieve (SNFS) is faster, it relies on the "special" shape of numbers like Fermat numbers and cannot be used to factor RSA moduli [4]. NFS is described as the fastest method available for RSA moduli [4].

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [4] Number crunching on clusters at EPFL, Number crunching on server clusters > Integer factorization > Approximate factoring run times
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA

**Grade:** correct

## q15: Did the authors trade inversions for multiplications when computing point addition in affine Weierstrass representation?

**Expected:** Yes. With simultaneous inversion over N parallel walks, one inversion is shared at the cost of about 3 extra multiplications per walk, so a point addition takes four multiplications, one squaring and 1/N of an inversion for x, plus one multiplication for y. On the SPU they used N = 400.

**Answer:**

Yes, the authors traded inversions for multiplications to avoid them in certain contexts, specifically by using projective representations or shared inversion techniques, although they noted that affine representation still requires an inversion per group operation.

According to [2], computing point addition in **affine Weierstrass representation** inherently requires "only a few multiplications plus an **inversion** (all in the prime field) per group operation." To avoid this cost, the authors discuss using **projective** representations: "With elliptic curves there is the issue how to represent group elements: affinely requiring only a few multiplications plus an inversion... or **projectively to avoid the inversion at the cost of more multiplications**."

Furthermore, in the context of parallel processing on an SPU (Source Unit Processor), [2] states that while affine representation requires inversions, "their inverses can be shared at the cost of, roughly, 3 additional multiplications per curve, using the simultaneous inversion from [31]." This implies a trade-off where the computational cost of performing multiple inversions is replaced by sharing them (which still involves multiplication-heavy algorithms) or switching to projective forms that eliminate inversions entirely but increase the count of multiplications.

Regarding the specific formulas for affine coordinates provided in [3], point addition ($P_1 \neq P_2$) requires computing $\lambda = \frac{y_2-y_1}{x_2-x_1}$, which involves a modular division (inversion). Point doubling ($P_1 = P_2$) requires $\lambda = \frac{3x_1^2+a}{2y_1}$, also involving inversion. The text in [3] explicitly identifies the "critical operation" as the "modular division," confirming that inversions are present and costly in affine arithmetic.

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.4 Incorporation into the Pollard rho iteration
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.4 Incorporation into the Pollard rho iteration
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.2 Modular arithmetic

**Grade:** partial

## q11: In which year was a 512-bit RSA modulus first factored?

**Expected:** 1999, with the Number Field Sieve.

**Answer:**

A 512-bit RSA modulus was first factored in **1999** [2][4].

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA

**Grade:** correct
