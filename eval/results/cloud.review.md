# Review: cloud

For each question, replace `?` after **Grade:** with `correct`, `partial` or `incorrect`.
For questions the papers do not answer, `correct` means the system said so.

## q01: Why does Bipartite Modular Multiplication give the correct result?

**Expected:** In the KT-residue system the multiplier is split as $Y = Y_H r^{\alpha n} + Y_L$, and $X \circledast Y = (X Y_H \bmod M + X Y_L r^{-\alpha n} \bmod M) \bmod M$, which equals $X Y r^{-\alpha n} \bmod M$. The interleaved algorithm computes the $Y_H$ term and the Montgomery algorithm the $Y_L$ term, in parallel.

**Answer:**

Bipartite Modular Multiplication splits the multiplier as $Y=Y_H r^{\alpha n}+Y_L$ and computes the two parts separately: $X Y_H \bmod M$ and $X Y_L r^{-\alpha n}\bmod M$. Adding them modulo $M$ gives
$$
X Y_H+X Y_L r^{-\alpha n}
= X(Y_H r^{\alpha n}+Y_L)r^{-\alpha n}
= X Y r^{-\alpha n}\pmod M.
$$
Thus the algorithm produces the specified result in the $KT$-residue system. Since $r^{\alpha n}$ is relatively prime to $M$, its modular inverse $r^{-\alpha n}$ exists. [4][6]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Abstract
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Pipelined Bipartite Modular Multiplication
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Introduction
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method

**Grade:** correct

## q02: What is the difference between modular division based on the extended binary GCD algorithm and modular division based on the extended Euclidean algorithm?

**Expected:** The extended Euclidean algorithm reduces A and B through integer divisions (quotient q, A - qB); the extended binary GCD algorithm uses only halving of even values and subtraction of odd ones. Both apply the same operations to U and V modulo M. In the thesis the binary GCD divider is a radix-4/2 design (MQRTR) combined with Montgomery multiplication; the Euclidean one improves Takagi's divider by sharing the modular reduction hardware and also computes ordinary modular multiplication.

**Answer:**

Both algorithms compute $Z=X/Y \bmod M$ by tracking the GCD calculation alongside corresponding updates to modular-quotient variables. Their main difference is how they perform the GCD steps:

- **Extended binary GCD:** uses parity, shifts, and subtraction. When $A$ is even, it replaces $A$ by $A/2$ and updates the quotient state with $U:=U/2 \bmod M$; when both operands are odd, it subtracts the smaller from the larger and makes corresponding modular updates. [1]
- **Extended Euclidean:** uses integer division. Each iteration computes $q=\lfloor A/B\rfloor$ and the remainder $A'=A-qB$, then updates the quotient state using $U'=U-qV \bmod M$. [5]

Thus, the binary-GCD method avoids general quotient/remainder steps in favor of shifts and subtractions, while the Euclidean method uses quotient-and-remainder divisions. Both rely on $\gcd(Y,M)=1$ to ensure the final modular quotient is obtained. [1][5]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Preliminaries > Modular Division > Extended Binary GCD Algorithm for Modular Division
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Binary GCD Algorithm > Similarity Between the Extended Binary GCD Algorithm and the Montgomery Multiplication Algorithm
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Binary GCD Algorithm > Introduction
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Hardware Algorithms for Modular Multiplication and Division Based on the Extended Eucliden Algorithm > An Improved Hardware Algorithm for Modular Division Based on the Extended Euclidean Algorithm
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Preliminaries > Modular Division > Extended Euclidean Algorithm for Modular Division
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Introduction

**Grade:** correct

## q03: What is the difference between the Montgomery multiplication algorithm and the interleaved modular multiplication algorithm?

**Expected:** Interleaved modular multiplication computes $X Y \bmod M$, processing the multiplier from the most significant digit and estimating the quotient $\lfloor Z/M \rfloor$. Montgomery multiplication works on M-residues of an odd modulus, computes $X Y R^{-1} \bmod M$ and processes the multiplier from the least significant digit, choosing a multiple of M that makes Z divisible by r, with no carry propagation in the quotient. The two are symmetric.

**Answer:**

The interleaved algorithm processes the multiplier’s digits from most significant to least significant. At each step it shifts the accumulated result by the radix $r$, adds a digit times $X$, and reduces modulo $M$. It computes $X\cdot Y \bmod M$ directly. [5]

The Montgomery algorithm processes digits from least significant to most significant. At each step it adds a digit times $X$, chooses $q_M$ to make the result divisible by $r$, and divides by $r$. It computes $X\cdot Y\cdot r^{-n}\bmod M$, so its result is in Montgomery representation. [5]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Introduction
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Preliminaries > Similarity Between the Extended Euclidean Algorithm and the Interleaved Modular Multiplication Algorithm
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Hardware Implementation
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Symmetry Between the Interleaved Modular Multiplication Algorithm and the Montgomery Multiplication Algorithm
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Abstract

**Grade:** correct

## q04: How does addition work in the binary signed-digit (SD2) representation?

**Expected:** Digits are in $\{\bar 1, 0, 1\}$. For each position an interim sum $h_i$ and a carry $c_i$ are computed from $a_i, b_i$ and the preceding digits (Table 2.1), then $s_i = h_i + c_{i-1}$. There is no carry propagation and all digits are computed in parallel.

**Answer:**

SD2 addition is performed without carry propagation. For each digit position, the two input digits are used to determine an interim sum $h_i$ and carry digit $c_i$; the result digit is then $s_i=h_i+c_{i-1}$. The digits can be computed in parallel, using the current input digits and nearby preceding digits to determine the interim values. [1]

Subtraction is done by changing the signs of the nonzero digits of one operand and then adding. Converting the SD2 result to an ordinary binary representation requires a carry-propagate addition. [1]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Preliminaries > Binary Signed-Digit Number Representation
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Preliminaries > Basic Operations in SD2 System
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Binary GCD Algorithm > Hardware Algorithms for Modular Multiplication and Division Based on the Extended Binary GCD Algorithm > A Hardware Algorithm for Montgomery Modular Multiplication
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > A Radix-$4$ Implementation Example
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > A Radix-$4$ Implementation Example
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Hardware Algorithms for Modular Multiplication and Division Based on the Extended Eucliden Algorithm > An Improved Hardware Algorithm for Modular Division Based on the Extended Euclidean Algorithm

**Grade:** partial

## q05: Can the multiplier be split into more than two parts in Bipartite Modular Multiplication?

**Expected:** Not covered: the thesis only describes splitting the multiplier into two parts, $Y_H$ and $Y_L$ (possibly of unequal length via $\alpha$). A good answer says the sources do not discuss more parts.

**Answer:**

The described Bipartite Modular Multiplication method splits the multiplier into two parts: an upper part processed by interleaved modular multiplication and a lower part processed by the Montgomery algorithm, in parallel. The sources do not say whether it can be generalized to more than two parts. [2][3]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Abstract
- [2] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Introduction
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Bipartite Modular Multiplication Method
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Hardware Implementation
- [5] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Bipartite Modular Multiplication > Hardware Implementation

**Grade:** correct

## q06: How many PlayStation 3 consoles did LACAL use to solve the 112-bit prime field ECDLP?

**Expected:** More than 215 PS3s (about 1300 SPUs). It took about half a year and was completed on July 8, 2009.

**Answer:**

LACAL used a cluster of 215 PlayStation 3 consoles to solve the 112-bit prime-field ECDLP. [2]

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.5 Timings
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC

**Grade:** partial

## q07: What is the birthday paradox, and why does it matter for Pollard's rho method?

**Expected:** 23 randomly picked people give a better than 50% chance that two share a birthday. Drawing with replacement from N objects, a repeat is expected after about $\sqrt{\pi N/2}$ draws, so Pollard's rho expects a collision after about $\sqrt{\pi q/2}$ steps.

**Answer:**

The birthday paradox is the unexpectedly high chance of finding a duplicate among random selections: with 23 people, there is more than a 50% chance that two share a birthday. More generally, sampling with replacement from $N$ possibilities takes about $\sqrt{\pi N/2}$ rounds, on average, to encounter a duplicate. [1]

Pollard’s rho uses this collision effect: it generates group elements in a random walk, and a duplicate is expected after about $\sqrt{\pi q/2}$ steps for a group of order $q$. In the discrete-log setting, a suitable collision gives an equation from which the logarithm can be recovered. [3]

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > The birthday paradox
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Pollard’s rho method to compute discrete logarithms
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.4 Incorporation into the Pollard rho iteration
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [6] Number crunching on clusters at EPFL, Number crunching on server clusters > Integer factorization > Approximate factoring run times

**Grade:** correct

## q08: How does Pollard's rho method compute discrete logarithms?

**Expected:** It looks at $g^r y^s$ for random pairs $(r, s)$ (in practice a pseudo-random walk). By the birthday paradox a duplicate $g^r y^s = g^u y^v$ appears after about $\sqrt{\pi q/2}$ steps, giving $x = (r-u)/(v-s) \bmod q$. It parallelises with distinguished points.

**Answer:**

Pollard’s rho searches for a collision among group elements represented as $g^r y^s$. When $g^r y^s=g^u y^v$ and $v\not\equiv s\pmod q$, the collision gives the logarithm
$$
x=\frac{r-u}{v-s}\pmod q.
$$
A random walk through the group generates the elements, while keeping track of their exponents; Floyd’s cycle-finding method can detect a collision by comparing points along the walk. [1]

For elliptic-curve groups, the same idea tracks points as linear combinations of the input points. For example, to find $m$ such that $Q=mP$, a walk updates $X_i=c_iP+d_iQ$ using selected fixed group elements. When a distinguished point is found more than once with different coefficients, the resulting relation can yield $m$. [5]

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Pollard’s rho method to compute discrete logarithms
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Discrete logarithms
- [4] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.4 Incorporation into the Pollard rho iteration
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.5 Timings

**Grade:** correct

## q09: Did LACAL solve the 160-bit prime field ECDLP?

**Expected:** No. The paper estimates about $2.5 \cdot 10^9$ PS3 years (a year on 2.5 billion devices) and considers an open-community attack even by 2020 very unlikely. LACAL solved the 112-bit one.

**Answer:**

No. The computation completed by LACAL was for a discrete logarithm over a **112-bit** prime field, not a 160-bit field. The paper estimates that cracking 160-bit ECC would require vastly more computation; it does not report that LACAL solved the 160-bit ECDLP. [4][6]

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 4 Conclusion
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.5 Timings

**Grade:** correct

## q10: Was the 112-bit prime field ECDLP computation supported by Sony?

**Expected:** Not stated: neither paper says Sony supported it. The acknowledged support is the Swiss National Science Foundation, EPFL DIT and the EU ICT program ECRYPT II.

**Answer:**

The sources do not say whether Sony supported the 112-bit prime-field ECDLP computation. They say the work used PS3 consoles and Sony’s hypervisor, but do not identify Sony as a supporter or sponsor. [1][2]

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.5 Timings
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 4 Conclusion
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC

**Grade:** correct

## q11: In which year was a 512-bit RSA modulus first factored?

**Expected:** 1999, with the Number Field Sieve.

**Answer:**

A 512-bit RSA modulus was first factored in 1999. [2]

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [6] Number crunching on clusters at EPFL, Introduction

**Grade:** correct

## q12: How does cracking 160-bit ECC compare with cracking 1024-bit RSA?

**Expected:** Cracking 160-bit ECC is at least three orders of magnitude harder. 160-bit ECC gives about 86-87 bits of security, 1024-bit RSA about 76 bits.

**Answer:**

The paper estimates that cracking 160-bit ECC is at least three orders of magnitude harder computationally than cracking 1024-bit RSA. It estimates about $2.5 \cdot 10^9$ PS3-years for 160-bit ECC, roughly a year using 2.5 billion devices; the comparison is based on the estimated effort for factoring a 1024-bit RSA modulus. [1][4]

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 4 Conclusion
- [3] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 1 Introduction
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [6] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, Front matter

**Grade:** correct

## q13: Can the PlayStation 3's graphics card be used to solve the ECDLP?

**Expected:** Not at the time: with the hypervisor version then current it was not accessible to applications other than games. If it were accessible it could double a PS3's effectiveness.

**Answer:**

Not with the PS3 setup described: the graphics card was inaccessible to applications other than gaming under the then-current hypervisor. The authors estimated that, if it were accessible, it could double a PS3’s effectiveness, but did not report using it to solve the ECDLP. [2]

**Sources:**

- [1] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Elliptic curve groups
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [5] Number crunching on clusters at EPFL, Introduction
- [6] Number crunching on clusters at EPFL, Number crunching on PlayStation 3 game consoles > Slicing and dicing on the SPU > Discrete logarithms

**Grade:** correct

## q14: What is the most effective published method for factoring an RSA modulus?

**Expected:** The Number Field Sieve (NFS).

**Answer:**

The most effective published method for factoring an RSA modulus is the Number Field Sieve (NFS). The Special Number Field Sieve (SNFS) is faster but applies only to specially shaped numbers, not RSA moduli. [1][3]

**Sources:**

- [1] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [3] Number crunching on clusters at EPFL, Number crunching on server clusters > Integer factorization > Approximate factoring run times
- [4] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 2 The security of 1024-bit RSA
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction

**Grade:** correct

## q15: Did the authors trade inversions for multiplications when computing point addition in affine Weierstrass representation?

**Expected:** Yes. With simultaneous inversion over N parallel walks, one inversion is shared at the cost of about 3 extra multiplications per walk, so a point addition takes four multiplications, one squaring and 1/N of an inversion for x, plus one multiplication for y. On the SPU they used N = 400.

**Answer:**

Yes. They use simultaneous inversion to share the inversion cost across $N$ parallel processes, at the cost of roughly three additional multiplications per curve. In affine Weierstrass representation, point addition then uses four multiplications and one squaring for the $x$-coordinate, plus one multiplication for the $y$-coordinate, and a $1/N$-th inversion. [2]

**Sources:**

- [1] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [2] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, 3 The security of 160-bit ECC
- [3] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Introduction
- [4] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, Abstract
- [5] On the Security of 1024-bit RSA and 160-bit Elliptic Curve Cryptography, A Multi-precision integer arithmetic on the SPU > A.4 Incorporation into the Pollard rho iteration
- [6] Studies on Hardware Algorithms and Architectures for Modular Arithmetic, A Hardware Algorithm for Modular Multiplication/Division Based on the Extended Euclidean Algorithm > Concluding Remarks

**Grade:** correct
