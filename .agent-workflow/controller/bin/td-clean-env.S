.global _start
.section .text
_start:
    lea 16(%rsp), %rdx
    mov %rdx, %rcx
1:
    mov (%rcx), %rdi
    test %rdi, %rdi
    jz 2f
    cmpb $45, (%rdi)
    jne 3f
    cmpb $45, 1(%rdi)
    jne 3f
    cmpb $0, 2(%rdi)
    je 4f
3:
    add $8, %rcx
    jmp 1b
4:
    movq $0, (%rcx)
    add $8, %rcx
    mov (%rcx), %rdi
    test %rdi, %rdi
    jz 2f
    mov %rcx, %rsi
    mov $59, %rax
    syscall
2:
    mov $127, %rdi
    mov $60, %rax
    syscall
