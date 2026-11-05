\\ Count primes p < N with p = 1 mod 4, and check Fermat's little theorem.
\p 38
N = 10^4;
count(N) = my(c = 0); forprime(p = 2, N, if (p % 4 == 1, c++)); c;
/* a block
   comment */
fermat(a, p) = Mod(a, p)^(p - 1) == 1;
s = "a \"quoted\" string";
v = vector(10, i, i^2 \ 3);
for (k = 1, 5, print(k, " ", fermat(2, prime(k + 1))));
x = 1.5e-3 + 0x1F + 0b101;
f(x) = x' + 2;
  \p 50
?ellinit
