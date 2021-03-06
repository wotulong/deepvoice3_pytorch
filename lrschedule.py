import numpy as np


# https://github.com/tensorflow/tensor2tensor/issues/280#issuecomment-339110329
def noam_learning_rate_decay(init_lr, global_step):
     # Noam scheme from tensor2tensor:
    warmup_steps = 4000.0
    step = global_step + 1.
    lr = init_lr * warmup_steps**0.5 * np.minimum(
        step * warmup_steps**-1.5, step**-0.5)
    return lr


def step_learning_rate_decay(init_lr, global_step,
                             anneal_rate=0.98,
                             anneal_interval=30000):
    return init_lr * anneal_rate ** (global_step // anneal_interval)
