import tensorflow as tf
from tensorflow.contrib.seq2seq.python.ops import attention_wrapper
from tensorflow.contrib.seq2seq.python.ops.attention_wrapper import LuongAttention, \
    AttentionWrapper, AttentionWrapperState


def _compute_attention(attention_mechanism, cell_output, attention_state,
                       attention_layer):
    print('THIS IS A SUUUUPER HACK FOR OVERRIDING SOMEONE OTHERS SHIT!')

    """Computes the attention and alignments for a given attention_mechanism."""
    alignments, next_attention_state = attention_mechanism(
        cell_output, state=attention_state)

    # Reshape from [batch_size, memory_time] to [batch_size, 1, memory_time]
    expanded_alignments = tf.expand_dims(alignments, 1)

    context_windows = []
    padded_alignment_windows = []

    window_start = attention_mechanism.window_start
    window_stop = attention_mechanism.window_stop

    pre_padding = attention_mechanism.window_pre_padding
    post_padding = attention_mechanism.window_post_padding

    full_pre_padding = attention_mechanism.full_seq_pre_padding
    full_post_padding = attention_mechanism.full_seq_post_padding

    def __process_entry(i):
        value_window = attention_mechanism.values[i, window_start[i][0]:window_stop[i][0], :]
        value_window_paddings = [
            [pre_padding[i][0], post_padding[i][0]],
            [0, 0]
        ]
        value_window = tf.pad(value_window, value_window_paddings, 'CONSTANT')
        value_window.set_shape((attention_mechanism.window_size, 256))

        context_window = tf.matmul(expanded_alignments[i], value_window)

        alignment_seq_paddings = [
            [full_pre_padding[i][0], full_post_padding[i][0]],
        ]

        # point_dist = tf.cast(tf.range(start=window_start[i][0],
        #                               limit=window_stop[i][0],
        #                               delta=1), dtype=tf.float32) - p[i][0]

        # gaussian_weights = tf.exp(-(point_dist ** 2) / 2 * (d / 2) ** 2)

        __alignments = tf.pad(alignments[i], alignment_seq_paddings, 'CONSTANT')

        return context_window, __alignments

    tmp_data = tf.map_fn(
        __process_entry,
        tf.range(start=0, limit=4, delta=1, dtype=tf.int32),
        dtype=(tf.float32, tf.float32),
        parallel_iterations=32)

    context = tmp_data[0]
    context = tf.Print(context, [tf.shape(context)], '_compute_attention context.matmul:')

    context = tf.squeeze(context, [1])
    context = tf.Print(context, [tf.shape(context)], '_compute_attention context.squeeze:')

    padded_alignment = tmp_data[1]
    padded_alignment = tf.Print(padded_alignment, [tf.shape(padded_alignment)],
                                '_compute_attention padded_alignments:')

    if attention_layer is not None:
        attention = attention_layer(tf.concat([cell_output, context], 1))
    else:
        attention = context

    return attention, padded_alignment, padded_alignment


attention_wrapper._compute_attention = _compute_attention


class LocalLuongAttention(LuongAttention):
    def __init__(self, num_units,
                 memory,
                 memory_sequence_length=None,
                 scale=False,
                 probability_fn=None,
                 score_mask_value=None,
                 dtype=None,
                 name="LocalLuongAttention"):
        # TODO: What about the query_layer in _BaseAttentionMechanism?
        super().__init__(num_units=num_units,
                         memory=memory,
                         memory_sequence_length=memory_sequence_length,
                         scale=scale,
                         probability_fn=probability_fn,
                         score_mask_value=score_mask_value,
                         dtype=dtype,
                         name=name)

    def __call__(self, query, state):
        with tf.variable_scope(None, "local_luong_attention", [query]) as test:
            # Get the depth of the memory values.
            num_units = self._keys.get_shape()[-1]

            # Get the source sequence length from memory.
            source_seq_length = tf.shape(self._keys)[1]

            # Predict p_t ==========================================================================
            vp = tf.get_variable(name="local_v_p", shape=[num_units, 1], dtype=tf.float32)
            wp = tf.get_variable(name="local_w_p", shape=[num_units, num_units], dtype=tf.float32)

            # shape => (B, num_units)
            _intermediate_result = tf.transpose(tf.tensordot(wp, query, [0, 1]))

            # shape => (B, 1)
            _tmp = tf.transpose(tf.tensordot(vp, tf.tanh(_intermediate_result), [0, 1]))

            _intermediate_prob = tf.sigmoid(_tmp)

            # p_t as described by Luong for the predictive local-p case.
            # self.p = tf.cast(source_seq_length, tf.float32) * _intermediate_prob
            # self.p = tf.Print(self.p, [self.p], 'LocalAttention self.p:', summarize=99)

            # p_t as described by Luong for the predictive local-m case.
            self.p = tf.tile(
                [[self.time]],
                tf.convert_to_tensor([4, 1])
            )

            self.p = tf.maximum(self.p, 10)
            self.p = tf.minimum(self.p, source_seq_length - 11)

            self.p = tf.cast(self.p, dtype=tf.float32)
            # ======================================================================================

            # TODO: Refactor this variables into separate hyper-parameters.
            self.d = 10
            self.window_size = 2 * self.d + 1

            start_index = tf.cast(self.p - self.d, dtype=tf.int32)
            self.window_start = tf.maximum(0, start_index)

            stop_index = tf.cast(self.p + self.d + 1, dtype=tf.int32)
            self.window_stop = tf.minimum(source_seq_length, stop_index)

            self.full_seq_pre_padding = tf.abs(start_index)
            self.full_seq_post_padding = tf.abs(stop_index - source_seq_length)

            self.window_pre_padding = tf.abs(self.window_start - start_index)
            self.window_post_padding = tf.abs(self.window_stop - stop_index)

            def __process_entry(i):
                __window = self._keys[i, self.window_start[i][0]:self.window_stop[i][0], :]

                paddings = [
                    [self.window_pre_padding[i][0], self.window_post_padding[i][0]],
                    [0, 0]
                ]
                return tf.pad(__window, paddings, 'CONSTANT')

            window = tf.map_fn(
                __process_entry,
                tf.range(start=0, limit=4, delta=1, dtype=tf.int32),
                dtype=(tf.float32),
                parallel_iterations=32)

            score = _local_luong_score(query, window, self._scale)

        score = tf.Print(score, [tf.shape(window)], 'LocalAttention window:', summarize=99)
        score = tf.Print(score, [tf.shape(self._keys)], 'LocalAttention _keys:')

        alignments = self._probability_fn(score, state)
        next_state = alignments

        alignments = tf.Print(alignments, [tf.shape(alignments)], 'LocalAttention alignments:')

        return alignments, next_state


def _local_luong_score(query, keys, scale):
    # TODO: Implement the "location" version ("dot": current, "general" and "concat" are also possible).
    # TODO: In the local version the tensor no longer contains max_time states but only 2D+1 ones.
    """Implements Luong-style (multiplicative) scoring function.

    This attention has two forms.  The first is standard Luong attention,
    as described in:

    Minh-Thang Luong, Hieu Pham, Christopher D. Manning.
    "Effective Approaches to Attention-based Neural Machine Translation."
    EMNLP 2015.  https://arxiv.org/abs/1508.04025

    The second is the scaled form inspired partly by the normalized form of
    Bahdanau attention.

    To enable the second form, call this function with `scale=True`.

    Args:
      query: Tensor, shape `[batch_size, num_units]` to compare to keys.
      keys: Processed memory, shape `[batch_size, max_time, num_units]`.
      scale: Whether to apply a scale to the score function.

    Returns:
      A `[batch_size, max_time]` tensor of unnormalized score values.

    Raises:
      ValueError: If `key` and `query` depths do not match.
    """
    depth = query.get_shape()[-1]
    key_units = keys.get_shape()[-1]
    if depth != key_units:
        raise ValueError(
            "Incompatible or unknown inner dimensions between query and keys.  "
            "Query (%s) has units: %s.  Keys (%s) have units: %s.  "
            "Perhaps you need to set num_units to the keys' dimension (%s)?"
            % (query, depth, keys, key_units, key_units))
    dtype = query.dtype

    query = tf.expand_dims(query, 1)
    score = tf.matmul(query, keys, transpose_b=True)
    score = tf.squeeze(score, [1])

    if scale:
        # Scalar used in weight scaling
        g = tf.get_variable(
            "attention_g", dtype=dtype,
            initializer=tf.ones_initializer, shape=())
        score = g * score

    return score


# http://cnyah.com/2017/08/01/attention-variants/


class AdvAttentionWrapper(AttentionWrapper):
    def __init__(self,
                 cell,
                 attention_mechanism,
                 attention_layer_size=None,
                 alignment_history=False,
                 cell_input_fn=None,
                 output_attention=True,
                 initial_cell_state=None,
                 name=None):
        super().__init__(cell=cell,
                         attention_mechanism=attention_mechanism,
                         attention_layer_size=attention_layer_size,
                         alignment_history=alignment_history,
                         cell_input_fn=cell_input_fn,
                         output_attention=output_attention,
                         initial_cell_state=initial_cell_state,
                         name=name)

    def call(self, inputs, state):
        """Perform a step of attention-wrapped RNN.

        - Step 1: Mix the `inputs` and previous step's `attention` output via
          `cell_input_fn`.
        - Step 2: Call the wrapped `cell` with this input and its previous state.
        - Step 3: Score the cell's output with `attention_mechanism`.
        - Step 4: Calculate the alignments by passing the score through the
          `normalizer`.
        - Step 5: Calculate the context vector as the inner product between the
          alignments and the attention_mechanism's values (memory).
        - Step 6: Calculate the attention output by concatenating the cell output
          and context through the attention layer (a linear layer with
          `attention_layer_size` outputs).

        Args:
          inputs: (Possibly nested tuple of) Tensor, the input at this time step.
          state: An instance of `AttentionWrapperState` containing
            tensors from the previous time step.

        Returns:
          A tuple `(attention_or_cell_output, next_state)`, where:

          - `attention_or_cell_output` depending on `output_attention`.
          - `next_state` is an instance of `AttentionWrapperState`
             containing the state calculated at this time step.

        Raises:
          TypeError: If `state` is not an instance of `AttentionWrapperState`.
        """
        if not isinstance(state, AttentionWrapperState):
            raise TypeError("Expected state to be instance of AttentionWrapperState. "
                            "Received type %s instead." % type(state))

        # Step 1: Calculate the true inputs to the cell based on the
        # previous attention value.
        cell_inputs = self._cell_input_fn(inputs, state.attention)
        cell_state = state.cell_state
        cell_output, next_cell_state = self._cell(cell_inputs, cell_state)

        cell_batch_size = (
                cell_output.shape[0].value or tf.shape(cell_output)[0])
        error_message = (
                "When applying AttentionWrapper %s: " % self.name +
                "Non-matching batch sizes between the memory "
                "(encoder output) and the query (decoder output).  Are you using "
                "the BeamSearchDecoder?  You may need to tile your memory input via "
                "the tf.contrib.seq2seq.tile_batch function with argument "
                "multiple=beam_width.")
        with tf.control_dependencies(
                self._batch_size_checks(cell_batch_size, error_message)):
            cell_output = tf.identity(
                cell_output, name="checked_cell_output")

        if self._is_multi:
            previous_attention_state = state.attention_state
            previous_alignment_history = state.alignment_history
        else:
            previous_attention_state = [state.attention_state]
            previous_alignment_history = [state.alignment_history]

        all_alignments = []
        all_attentions = []
        all_attention_states = []
        maybe_all_histories = []
        for i, attention_mechanism in enumerate(self._attention_mechanisms):
            attention_mechanism.time = state.time
            attention, alignments, next_attention_state = _compute_attention(
                attention_mechanism, cell_output, previous_attention_state[i],
                self._attention_layers[i] if self._attention_layers else None)
            alignment_history = previous_alignment_history[i].write(
                state.time, alignments) if self._alignment_history else ()

            all_attention_states.append(next_attention_state)
            all_alignments.append(alignments)
            all_attentions.append(attention)
            maybe_all_histories.append(alignment_history)

        attention = tf.concat(all_attentions, 1)
        next_state = AttentionWrapperState(
            time=state.time + 1,
            cell_state=next_cell_state,
            attention=attention,
            attention_state=self._item_or_tuple(all_attention_states),
            alignments=self._item_or_tuple(all_alignments),
            alignment_history=self._item_or_tuple(maybe_all_histories))

        if self._output_attention:
            return attention, next_state
        else:
            return cell_output, next_state