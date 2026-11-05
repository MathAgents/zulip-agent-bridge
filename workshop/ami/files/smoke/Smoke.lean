import Mathlib

theorem zab_smoke (n : ℕ) : Even (n * (n + 1)) := Nat.even_mul_succ_self n

example : Irrational (Real.sqrt 2) := irrational_sqrt_two

#eval (2 : ℤ) ^ 10
