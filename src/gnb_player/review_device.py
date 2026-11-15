"""Explicit optional review device policy. CPU is the unchanged default."""

def select_device(torch, config):
    mode=config.get('review_device','cpu')
    precision=config.get('review_precision','float32')
    if mode not in {'cpu','rocm'}:
        raise ValueError('Choose cpu or rocm for review_device.')
    if precision not in {'float32','float16','bfloat16'}:
        raise ValueError('Choose float32, float16 or bfloat16 for review_precision.')
    if mode=='cpu':
        if precision!='float32':raise ValueError('The preserved CPU review path requires float32.')
        return {'dtype':torch.float32,'device_map':'cpu'}, {'device':'cpu','dtype':'float32','backend':'CPU FP32'}
    if not getattr(torch.version,'hip',None):
        raise RuntimeError('AMD GPU review requires ROCm PyTorch in the selected review runtime. Select CPU explicitly to use the CPU fallback.')
    if not torch.cuda.is_available():
        raise RuntimeError('The selected review runtime cannot access an AMD GPU. No silent CPU fallback was started.')
    properties=torch.cuda.get_device_properties(0)
    return {'dtype':getattr(torch,precision),'device_map':'cuda:0'}, {
        'device':'cuda:0','dtype':precision,'backend':'AMD ROCm','hip':torch.version.hip,
        'gpu_name':properties.name,'gpu_arch':getattr(properties,'gcnArchName',''),
        'gpu_total_memory':properties.total_memory}
