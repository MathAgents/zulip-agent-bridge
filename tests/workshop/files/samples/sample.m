// Class groups of imaginary quadratic fields, and a small intrinsic.
/* a block
   comment */
intrinsic SmallClassNumbers(B::RngIntElt) -> SeqEnum
{ Discriminants -d with h(-d) = 1 and d < B. }
    return [d : d in [3..B] | IsFundamentalDiscriminant(-d) and
        ClassNumber(QuadraticField(-d)) eq 1];
end intrinsic;

function f(n)
    if n le 1 then return 1; elif n mod 2 eq 0 then return f(n div 2);
    else return f(3*n + 1); end if;
end function;

K<w> := QuadraticField(-23);
printf "h = %o\n", ClassNumber(K);
x := 1.5e-3; s := "a \"quoted\" string";
for p in PrimesUpTo(50) do vprint User1: p; end for;
s := &+[p : p in PrimesUpTo(100)]; t := &*[1..10]; u := &cat["a", "b"];
E := rec< recformat< Label > | Label := "37a1" >; E`Label := "37.a1"; print E`Label;
fact := func< n | n le 1 select 1 else n * $$(n - 1) >; plus := '+';
