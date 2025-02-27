# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import, division, print_function

import time
import paddle
from ppcls.engine.train.utils import update_loss, update_metric, log_info, type_name
from ppcls.utils import profiler


def train_epoch(engine, epoch_id, print_batch_step):
    tic = time.time()

    if not hasattr(engine, "train_dataloader_iter"):
        engine.train_dataloader_iter = iter(engine.train_dataloader)

    for iter_id in range(engine.iter_per_epoch):
        # fetch data batch from dataloader
        try:
            batch = next(engine.train_dataloader_iter)
        except Exception:
            # NOTE: reset DALI dataloader manually
            if engine.use_dali:
                engine.train_dataloader.reset()
            engine.train_dataloader_iter = iter(engine.train_dataloader)
            batch = next(engine.train_dataloader_iter)

        profiler.add_profiler_step(engine.config["profiler_options"])
        if iter_id == 5:
            for key in engine.time_info:
                engine.time_info[key].reset()
        engine.time_info["reader_cost"].update(time.time() - tic)

        batch_size = batch[0].shape[0]
        if not engine.config["Global"].get("use_multilabel", False):
            batch[1] = batch[1].reshape([batch_size, -1])
        engine.global_step += 1

        # image input
        if engine.amp:
            amp_level = engine.config["AMP"].get("level", "O1").upper()
            with paddle.amp.auto_cast(
                    custom_black_list={
                        "flatten_contiguous_range", "greater_than"
                    },
                    level=amp_level):
                out = forward(engine, batch)
                loss_dict = engine.train_loss_func(out, batch[1])
        else:
            out = forward(engine, batch)
            loss_dict = engine.train_loss_func(out, batch[1])

        # loss
        loss = loss_dict["loss"] / engine.update_freq

        # backward & step opt
        if engine.amp:
            scaled = engine.scaler.scale(loss)
            scaled.backward()
            if (iter_id + 1) % engine.update_freq == 0:
                for i in range(len(engine.optimizer)):
                    engine.scaler.minimize(engine.optimizer[i], scaled)
        else:
            loss.backward()
            if (iter_id + 1) % engine.update_freq == 0:
                for i in range(len(engine.optimizer)):
                    engine.optimizer[i].step()

        if (iter_id + 1) % engine.update_freq == 0:
            # clear grad
            for i in range(len(engine.optimizer)):
                engine.optimizer[i].clear_grad()
            # step lr(by step)
            for i in range(len(engine.lr_sch)):
                if not getattr(engine.lr_sch[i], "by_epoch", False):
                    engine.lr_sch[i].step()
            # update ema
            if engine.ema:
                engine.model_ema.update(engine.model)

        # below code just for logging
        # update metric_for_logger
        update_metric(engine, out, batch, batch_size)
        # update_loss_for_logger
        update_loss(engine, loss_dict, batch_size)
        engine.time_info["batch_cost"].update(time.time() - tic)
        if iter_id % print_batch_step == 0:
            log_info(engine, batch_size, epoch_id, iter_id)
        tic = time.time()

    # step lr(by epoch)
    for i in range(len(engine.lr_sch)):
        if getattr(engine.lr_sch[i], "by_epoch", False) and \
                type_name(engine.lr_sch[i]) != "ReduceOnPlateau":
            engine.lr_sch[i].step()


def forward(engine, batch):
    if not engine.is_rec:
        return engine.model(batch[0])
    else:
        return engine.model(batch[0], batch[1])
