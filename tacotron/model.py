import tensorflow as tf
import tensorflow.contrib as tfc
from tensorflow.contrib import seq2seq

from audio.conversion import inv_normalize_decibel, decibel_to_magnitude, ms_to_samples
from audio.synthesis import spectrogram_to_wav
from tacotron.helpers import TacotronInferenceHelper, TacotronTrainingHelper
from tacotron.layers import cbhg, pre_net
# TODO: Clean up document and comments.
from tacotron.wrappers import PrenetWrapper, ConcatOutputAndAttentionWrapper


class Tacotron:
    """
    Implementation of the Tacotron architecture as described in
    "Tacotron: Towards End-to-End Speech Synthesis".

    See: "Tacotron: Towards End-to-End Speech Synthesis"
      * Source: [1] https://arxiv.org/abs/1703.10135
    """

    def __init__(self, hparams, inputs, training=True):
        """
        Creates an instance of the Tacotron model.

        Arguments:
            hparams (tf.contrib.training.HParams):
                Collection of hyper-parameters that control how the model is created.

            inputs (:obj:`list` of :obj:`tf.Tensor`):
                Input data placeholders. All data that is used for training or inference is
                consumed from this placeholders.
                # TODO: Describe all inputs and their shapes.

            training (boolean):
                Flag that controls the application of special architecture behaviour that only
                has to be applied during training. For example, this flags controls the
                application of dropout.
        """
        self.hparams = hparams

        # Get the placeholders for the input data.
        self.inp_sentences, self.seq_lengths, self.inp_mel_spec, self.inp_linear_spec, \
        self.inp_time_steps = inputs

        self.output_linear_spec = None

        # Merged loss function.
        self.loss_op = None
        # Mel. spectrogram loss measured after the decoder.
        self.loss_op_decoder = None
        # Linear spectrogram loss measured at the and of the network.
        self.loss_op_post_processing = None

        # Decoded Mel. spectrogram, shape=(B, T_spec, n_mels).
        self.output_mel_spec = None

        self.training = training

        # Construct the network.
        self.model()

    def encoder(self, inputs):
        """
        Implementation of the CBHG based Tacotron encoder network.

        Arguments:
            inputs (tf.Tensor):
            The shape is expected to be shape=(B, T_sent, ) with B being the batch size, T_sent
            being the number of tokens in the sentence including the EOS token.

        Returns:
         (outputs, output_states):
            outputs (tf.Tensor): The output states (output_fw, output_bw) of the RNN concatenated
                over time. Its shape is expected to be shape=(B, T_sent, 2 * n_gru_units) with B being
                the batch size, T_sent being the number of tokens in the sentence including the EOS
                token.

            output_states (tf.Tensor): A tensor containing the forward and the backward final states
                (output_state_fw, output_state_bw) of the bidirectional rnn.
                Its shape is expected to be shape=(B, 2, n_gru_units) with B being the batch size.
        """
        with tf.variable_scope('encoder'):
            char_embeddings = tf.get_variable("embedding", [
                self.hparams.vocabulary_size,
                self.hparams.encoder.embedding_size
            ],
                                              dtype=tf.float32,
                                              initializer=tf.glorot_uniform_initializer)

            # shape => (B, T_sent, 256)
            embedded_char_ids = tf.nn.embedding_lookup(char_embeddings, inputs)

            # shape => (B, T_sent, 128)
            network = pre_net(inputs=embedded_char_ids,
                              layers=self.hparams.encoder.pre_net_layers,
                              training=self.training)

            # network.shape => (B, T_sent, 2 * n_gru_units)
            # state.shape   => (2, n_gru_units)
            network, state = cbhg(inputs=network,
                                  n_banks=self.hparams.encoder.n_banks,
                                  n_filters=self.hparams.encoder.n_filters,
                                  n_highway_layers=self.hparams.encoder.n_highway_layers,
                                  n_highway_units=self.hparams.encoder.n_highway_units,
                                  projections=self.hparams.encoder.projections,
                                  n_gru_units=self.hparams.encoder.n_gru_units,
                                  training=self.training)

        return network, state

    def decoder(self, encoder_outputs, encoder_state):
        """
        Implementation of the Tacotron decoder network.

        Arguments:
            encoder_outputs (tf.Tensor):
                The output states of the encoder RNN concatenated over time. Its shape is
                expected to be shape=(B, T_sent, 2 * encoder.n_gru_units) with B being the batch
                size, T_sent being the number of tokens in the sentence including the EOS token.

            encoder_state (tf.Tensor):
                A tensor containing the forward and the backward final states
                (output_state_fw, output_state_bw) of the bidirectional RNN. Its shape is
                expected to be shape=(B, 2, encoder.n_gru_units) with B being the batch size.

        Returns:
            tf.tensor:
                Generated reduced Mel. spectrogram. The shape is
                shape=(B, T_spec // r, n_mels * r), with B being the batch size, T_spec being
                the number of frames in the spectrogram and r being the reduction factor.
        """
        with tf.variable_scope('decoder'):
            # Query the number of layers for the decoder RNN.
            n_decoder_layers = self.hparams.decoder.n_gru_layers

            # Query the number of units for the decoder cells.
            n_decoder_units = self.hparams.decoder.n_decoder_gru_units

            # Query the number of units for the attention cell.
            n_attention_units = self.hparams.decoder.n_attention_units

            # Determine the current batch size.
            batch_size = tf.shape(encoder_outputs)[0]

            # Create the attention mechanism.
            attention_mechanism = tfc.seq2seq.BahdanauAttention(
                num_units=n_attention_units,
                memory=encoder_outputs,
                memory_sequence_length=None,
                dtype=tf.float32
            )

            # Create the attention RNN cell.
            attention_cell = tf.nn.rnn_cell.GRUCell(num_units=n_attention_units,
                                                    name='attention_gru_cell')

            # Apply the pre-net to each decoder input as show in [1], figure 1.
            attention_cell = PrenetWrapper(attention_cell,
                                           self.hparams.decoder.pre_net_layers,
                                           self.training)

            # Connect the attention cell with the attention mechanism.
            wrapped_attention_cell = tfc.seq2seq.AttentionWrapper(
                cell=attention_cell,
                attention_mechanism=attention_mechanism,
                attention_layer_size=None,
                alignment_history=True,
                output_attention=False,  # True for Luong-style att., False for Bhadanau-style.
                initial_cell_state=None
            )  # => (B, T_sent, n_attention_units) = (B, T_sent, 256)

            # ======================================================================================
            # NOTE: This is actually derived from the Tacotron 2 paper and only an experiment.
            # ======================================================================================
            # => (B, T_sent, n_attention_units * 2) = (B, T_sent, 512)
            concat_cell = ConcatOutputAndAttentionWrapper(wrapped_attention_cell)

            # => (B, T_sent, n_decoder_units) = (B, T_sent, 256)
            concat_cell = tfc.rnn.OutputProjectionWrapper(concat_cell, n_decoder_units)
            # ======================================================================================

            # Stack several GRU cells and apply a residual connection after each cell.
            # Before the input reaches the decoder RNN it passes through the attention cell.
            cells = [concat_cell]
            for i in range(n_decoder_layers):
                # => (B, T_spec, n_decoder_units) = (B, T_spec, 256)
                cell = tf.nn.rnn_cell.GRUCell(num_units=n_decoder_units, name='gru_cell')
                # => (B, T_spec, n_decoder_units) = (B, T_spec, 256)
                cell = tf.nn.rnn_cell.ResidualWrapper(cell)
                cells.append(cell)

            # => (B, T_spec, n_decoder_units) = (B, T_spec, 256)
            decoder_cell = tf.nn.rnn_cell.MultiRNNCell(cells, state_is_tuple=True)

            # Project the final cells output to the decoder target size.
            # => (B, T_spec, target_size * reduction) = (B, T_spec, 80 * reduction)
            output_cell = tfc.rnn.OutputProjectionWrapper(
                cell=decoder_cell,
                output_size=self.hparams.decoder.target_size * self.hparams.reduction,
                activation=tf.nn.sigmoid
            )

            # TODO: Experiment with initialising using the encoder state:
            # ".clone(cell_state=encoder_state)"
            # Derived from: https://github.com/tensorflow/nmt/blob/365e7386e6659526f00fa4ad17eefb13d52e3706/nmt/attention_model.py#L131
            decoder_initial_state = output_cell.zero_state(
                batch_size=batch_size,
                dtype=tf.float32
            )

            if self.training:
                # Create a custom training helper for feeding ground truth frames during training.
                helper = TacotronTrainingHelper(
                    batch_size=batch_size,
                    inputs=encoder_outputs,
                    outputs=self.inp_mel_spec,
                    input_size=self.hparams.decoder.target_size,
                    reduction_factor=self.hparams.reduction
                )
            else:
                # Create a custom inference helper that handles proper data feeding.
                helper = TacotronInferenceHelper(batch_size=batch_size,
                                                 input_size=self.hparams.decoder.target_size)

            decoder = seq2seq.BasicDecoder(cell=output_cell,
                                           helper=helper,
                                           initial_state=decoder_initial_state)

            if self.training:
                # During training we do not stop decoding manually. The decoder automatically
                # decodes as many time steps as are contained in the ground truth data.
                maximum_iterations = None
            else:
                # During inference we stop decoding after `maximum_iterations`. Note that when
                # using the reduction factor the RNN actually outputs
                # `maximum_iterations` * `reduction_factor` frames.
                maximum_iterations = self.hparams.decoder.maximum_iterations

            # Start decoding.
            decoder_outputs, final_state, final_sequence_lengths = seq2seq.dynamic_decode(
                decoder=decoder,
                output_time_major=False,
                impute_finished=False,
                maximum_iterations=maximum_iterations)

            # decoder_outputs => type=BasicDecoderOutput, (rnn_output, _)
            # final_state => type=AttentionWrapperState, (attention_wrapper_state, _, _)
            # final_sequence_lengths.shape = (B)

            # Create an attention alignment summary image.
            Tacotron._create_attention_summary(final_state)

        # shape => (B, T_spec // r, n_mels * r)
        return decoder_outputs.rnn_output

    def post_process(self, inputs):
        """
        Apply the CBHG based post-processing network to the spectrogram.

        Arguments:
            inputs (tf.Tensor):
                The shape is expected to be shape=(B, T, n_mels) with B being the
                batch size and T being the number of time frames.

        Returns:
            tf.Tensor:
                A tensor which shape is expected to be shape=(B, T_spec, 2 * n_gru_units) with B
                being the batch size and T being the number of time frames.
        """
        with tf.variable_scope('post_process'):
            # network.shape => (B, T_spec, 2 * n_gru_units)
            # state.shape   => (2, n_gru_units)
            network, state = cbhg(inputs=inputs,
                                  n_banks=self.hparams.post.n_banks,
                                  n_filters=self.hparams.post.n_filters,
                                  n_highway_layers=self.hparams.post.n_highway_layers,
                                  n_highway_units=self.hparams.post.n_highway_units,
                                  projections=self.hparams.post.projections,
                                  n_gru_units=self.hparams.post.n_gru_units,
                                  training=self.training)

        return network

    def model(self):
        """
        Builds the Tacotron model.
        """
        # inp_sentences.shape = (B, T_sent, ?)
        batch_size = tf.shape(self.inp_sentences)[0]

        # network.shape => (B, T_sent, 256)
        # encoder_state.shape => (B, 2, 256)
        encoder_outputs, encoder_state = self.encoder(self.inp_sentences)

        # shape => (B, T_spec // r, n_mels * r)
        decoder_outputs = self.decoder(encoder_outputs, encoder_state)

        # shape => (B, T_spec, n_mels)
        decoder_outputs = tf.reshape(decoder_outputs, [batch_size, -1, self.hparams.n_mels])

        # shape => (B, T_spec, n_mels)
        self.output_mel_spec = decoder_outputs

        outputs = decoder_outputs
        if self.hparams.apply_post_processing:
            # shape => (B, T_spec, 256)
            outputs = self.post_process(outputs)

        # shape => (B, T_spec, (1 + n_fft // 2))
        outputs = tf.layers.dense(inputs=outputs,
                                  units=(1 + self.hparams.n_fft // 2),
                                  activation=tf.nn.sigmoid,
                                  kernel_initializer=tf.glorot_normal_initializer(),
                                  bias_initializer=tf.glorot_normal_initializer())

        # shape => (B, T_spec, (1 + n_fft // 2))
        self.output_linear_spec = outputs

        if self.training:
            # Calculate decoder Mel. spectrogram loss.
            self.loss_op_decoder = tf.reduce_mean(
                tf.abs(self.inp_mel_spec - self.output_mel_spec))

            # Calculate post-processing linear spectrogram loss.
            self.loss_op_post_processing = tf.reduce_mean(
                tf.abs(self.inp_linear_spec - self.output_linear_spec))

            # Combine the decoder and the post-processing losses.
            self.loss_op = self.loss_op_decoder + self.loss_op_post_processing

    def get_loss_op(self):
        """
        Get the models loss function.

        Returns:
            tf.Tensor
        """
        return self.loss_op

    def summary(self):
        """
        Create all summary operations for the model.

        Returns:
            tf.Tensor:
                A tensor of type `string` containing the serialized `Summary` protocol
                buffer containing all merged model summaries.
        """
        with tf.name_scope('loss'):
            tf.summary.scalar('loss', self.loss_op)
            tf.summary.scalar('loss_decoder', self.loss_op_decoder)
            tf.summary.scalar('loss_post_processing', self.loss_op_post_processing)

        with tf.name_scope('normalized_inputs'):
            # Convert the mel spectrogram into an image that can be displayed.
            # => shape=(1, T_spec, n_mels, 1)
            mel_spec_img = tf.expand_dims(
                tf.reshape(self.inp_mel_spec[0],
                           (1, -1, self.hparams.n_mels)), -1)

            # => shape=(n_mels, T_spec, 1)
            mel_spec_img = tf.transpose(mel_spec_img, perm=[0, 2, 1, 3])
            tf.summary.image('mel_spec', mel_spec_img, max_outputs=1)

            # Convert thew linear spectrogram into an image that can be displayed.
            # => shape=(1, T_spec, (1 + n_fft // 2), 1)
            linear_spec_image = tf.expand_dims(
                tf.reshape(self.inp_linear_spec[0],
                           (1, -1, (1 + self.hparams.n_fft // 2))), -1)

            # => shape=(1, T_spec, (1 + n_fft // 2), 1)
            linear_spec_image = tf.transpose(linear_spec_image, perm=[0, 2, 1, 3])
            tf.summary.image('linear_spec', linear_spec_image, max_outputs=1)

        with tf.name_scope('normalized_outputs'):
            # Convert the mel spectrogram into an image that can be displayed.
            # => shape=(1, T_spec, n_mels, 1)
            mel_spec_img = tf.expand_dims(
                tf.reshape(self.output_mel_spec[0],
                           (1, -1, self.hparams.n_mels)), -1)

            # => shape=(n_mels, T_spec, 1)
            mel_spec_img = tf.transpose(mel_spec_img, perm=[0, 2, 1, 3])
            tf.summary.image('decoder_mel_spec', mel_spec_img, max_outputs=1)

            # Convert thew linear spectrogram into an image that can be displayed.
            # => shape=(1, T_spec, (1 + n_fft // 2), 1)
            linear_spec_image = tf.expand_dims(
                tf.reshape(self.output_linear_spec[0],
                           (1, -1, (1 + self.hparams.n_fft // 2))), -1)

            # => shape=(1, T_spec, (1 + n_fft // 2), 1)
            linear_spec_image = tf.transpose(linear_spec_image, perm=[0, 2, 1, 3])
            tf.summary.image('linear_spec', linear_spec_image, max_outputs=1)

        # TODO: Turned off since it is only of used for debugging.
        if self.training is True and False:
            with tf.name_scope('inference_reconstruction'):
                win_len = ms_to_samples(self.hparams.win_len,
                                        sampling_rate=self.hparams.sampling_rate)
                win_hop = ms_to_samples(self.hparams.win_hop,
                                        sampling_rate=self.hparams.sampling_rate)
                n_fft = self.hparams.n_fft

                def __synthesis(spec):
                    print('synthesis ....', spec.shape)
                    linear_mag_db = inv_normalize_decibel(spec.T, 35.7, 100)
                    linear_mag = decibel_to_magnitude(linear_mag_db)

                    _wav = spectrogram_to_wav(linear_mag,
                                              win_len,
                                              win_hop,
                                              n_fft,
                                              50)

                    # save_wav('/tmp/reconstr.wav', _wav, hparams.sampling_rate, True)
                    return _wav

                reconstruction = tf.py_func(__synthesis, [self.output_linear_spec[0]], [tf.float32])

                tf.summary.audio('synthesized', reconstruction, self.hparams.sampling_rate)

        return tf.summary.merge_all()

    @staticmethod
    def _create_attention_summary(final_context_state):
        # TODO: Add documentation.
        attention_wrapper_state, unkn1, unkn2 = final_context_state

        cell_state, attention, _, alignments, alignment_history, attention_state = \
            attention_wrapper_state

        # print('cell_state', cell_state)
        # print('attention', attention)
        # print('alignments', alignments)
        # print('alignment_history', alignment_history)
        # print('attention_state', attention_state)
        #
        # print('unkn1', unkn1)
        # print('unkn2', unkn2)

        # tf.summary.image("cell_state", tf.expand_dims(tf.reshape(cell_state[0], (1, 1, 256)), -1))
        # tf.summary.image("attention", tf.expand_dims(tf.reshape(attention[0], (1, 1, 256)), -1))
        # tf.summary.image("alignments", tf.expand_dims(tf.expand_dims(alignments, -1), 0))
        # tf.summary.image("attention_state", tf.expand_dims(tf.expand_dims(attention_state, -1),0))

        # tf.summary.image("unkn1", tf.expand_dims(tf.reshape(unkn1[0], (1, 1, 256)), -1))
        # tf.summary.image("unkn2", tf.expand_dims(tf.reshape(unkn2[0], (1, 1, 256)), -1))

        stacked_alignment_hist = alignment_history.stack()
        stacked_alignments = tf.transpose(stacked_alignment_hist, [1, 2, 0])
        tf.summary.image("stacked_alignments", tf.expand_dims(stacked_alignments, -1))

        # === DEBUG ======================================================================
        # cell_state = tf.Print(cell_state, [tf.shape(cell_state)], 'cell_state.shape')
        # tf.summary.tensor_summary('cell_state', cell_state)
        #
        # attention = tf.Print(attention, [tf.shape(attention)], 'attention.shape')
        # tf.summary.tensor_summary('attention', attention)
        #
        # alignments = tf.Print(alignments, [tf.shape(alignments)], 'alignments.shape')
        # tf.summary.tensor_summary('alignments', alignments)
        #
        # attention_state = tf.Print(attention_state, [tf.shape(attention_state)],
        #   'attention_state.shape')
        # tf.summary.tensor_summary('attention_state', attention_state)
        #
        # unkn1 = tf.Print(unkn1, [tf.shape(unkn1)], 'unkn1.shape')
        # tf.summary.tensor_summary('unkn1', unkn1)
        #
        # unkn2 = tf.Print(unkn2, [tf.shape(unkn2)], 'unkn2.shape')
        # tf.summary.tensor_summary('unkn2', unkn2)
