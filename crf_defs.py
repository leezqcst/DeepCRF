from model_defs import *
from utils import *
from tensorflow.models.rnn.rnn_cell import *

###################################
# Building blocks                 #
###################################

# takes features and outputs potentials
def potentials_layer(in_layer, mask, config, params, reuse=False, name='Potentials'):
    batch_size = int(in_layer.get_shape()[0])
    num_steps = int(in_layer.get_shape()[1])
    input_size = int(in_layer.get_shape()[2])
    pot_shape = [config.n_tags] * config.pot_window
    out_shape = [batch_size, num_steps] + pot_shape
    #~ pot_size = config.n_tags ** config.pot_window
    #~ if reuse:
        #~ tf.get_variable_scope().reuse_variables()
        #~ W_pot = params.W_pot
        #~ b_pot = params.b_pot
    #~ else:
        #~ W_pot = weight_variable([input_size, pot_size], name=name)
        #~ b_pot = bias_variable([pot_size], name=name)
    #~ flat_input = tf.reshape(in_layer, [-1, input_size])
    #~ pre_scores = tf.matmul(flat_input, W_pot) + b_pot
    # BOGUS
    W_pot = False
    b_pot = False
    reshaped_in = tf.reshape(in_layer, [batch_size, num_steps, config.pot_window, -1])
    pre_scores = tf.reduce_sum(reshaped_in, 2)
    # /BOGUS
    pots_layer = tf.reshape(pre_scores, out_shape)
    # define potentials for padding tokens
    padding_pot = np.zeros(pot_shape)
    num = config.pot_window / 2
    idx = [slice(None)] * num + [0] + [slice(None)] * num
    padding_pot[idx] += 10000
    pad_pot = tf.convert_to_tensor(padding_pot, tf.float32)
    pad_pots = tf.expand_dims(tf.expand_dims(pad_pot, 0), 0)
    pad_pots = tf.tile(pad_pots, [batch_size, num_steps] + [1] * config.pot_window)
    # expand mask
    mask_a = mask
    for _ in range(config.pot_window):
        mask_a = tf.expand_dims(mask_a, -1)
    mask_a = tf.tile(mask_a, [1, 1] + pot_shape)
    # combine
    pots_layer = (pots_layer * mask_a + (1 - mask_a) * pad_pots)
    return (pots_layer, W_pot, b_pot)


# pseudo-likelihood criterion
def pseudo_likelihood(potentials, pot_indices, targets, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    # move the current tag to the last dimension
    perm = range(len(potentials.get_shape()))
    mid = config.pot_window / 2
    perm[-1] = perm[-mid - 1]
    for i in range(-1, mid -1):
        perm[-mid + i] = perm[-mid + i] + 1
    perm_potentials = tf.transpose(potentials, perm=perm)
    # get conditional distribution of the current tag
    flat_pots = tf.reshape(perm_potentials, [-1, config.n_tags])
    flat_cond = tf.gather(flat_pots, pot_indices)
    pre_cond = tf.nn.softmax(flat_cond)
    conditional = tf.reshape(pre_cond, [batch_size, num_steps, -1])
    # compute pseudo-log-likelihood of sequence
    p_ll = tf.reduce_sum(targets * tf.log(conditional))
    return (conditional, p_ll)


# dynamic programming part 1: max sum
class CRFMaxCell(RNNCell):
    """Dynamic programming for CRF"""
    def __init__(self, config):
        self._num_units = config.n_tags ** (config.pot_window - 1)
        self.n_tags = config.n_tags
    
    @property
    def input_size(self):
        return self._num_units

    @property
    def output_size(self):
        return self._num_units
    
    @property
    def state_size(self):
        return self._num_units
    
    def __call__(self, inputs, state, scope=None):
        """Summation for dynamic programming. Inputs are the
        log-potentials. States are the results of the summation at the
        last step"""
        with tf.variable_scope(scope or type(self).__name__):
            # add states and log-potentials
            multiples = [1] * (len(state.get_shape()) + 1)
            multiples[-1] = self.n_tags
            exp_state = tf.tile(tf.expand_dims(state, -1), multiples)
            added = exp_state + inputs
            # return maxes, arg_maxes along first dimension (after the batch dim)
            new_state = tf.reduce_max(added, 1)
            max_id = tf.argmax(added, 1)
        return new_state, max_id


# max a posteriori tags assignment: implement dynamic programming
def map_assignment(potentials, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    inputs_list = [tf.reshape(x, [batch_size] + pots_shape)
                   for x in tf.split(1, num_steps, potentials)]
    # forward pass
    max_cell = CRFMaxCell(config)
    max_ids = [0] * len(inputs_list)
    # initial state: starts at 0 - 0 - 0 etc...
    state = tf.zeros(pots_shape[:-1])
    for t, input_ in enumerate(inputs_list):
        state, max_id = max_cell(inputs_list[t], state)
        max_ids[t] = max_id
    # backward pass
    powers = tf.to_int64(map(float, range(batch_size))) * \
             (config.n_tags ** (config.pot_window - 1))
    outputs = [-1] * len(inputs_list)
    best_end = tf.argmax(tf.reshape(state, [batch_size, -1]), 1)
    current = best_end
    mid = config.pot_window / 2
    max_pow = (config.n_tags ** mid)
    for i, _ in enumerate(outputs):
        outputs[-1 - i] = (current / max_pow) 
        prev_best = tf.gather(tf.reshape(max_ids[-1 - i], [-1]), current + powers)
        current = prev_best * max_pow + (current / config.n_tags)
    map_tags = tf.transpose(tf.pack(outputs))
    return map_tags


# dynamic programming part 2: sum product
class CRFSumCell(RNNCell):
    """Dynamic programming for CRF"""
    def __init__(self, config):
        self._num_units = config.n_tags ** (config.pot_window - 1)
        self.n_tags = config.n_tags
    
    @property
    def input_size(self):
        return self._num_units

    @property
    def output_size(self):
        return self._num_units
    
    @property
    def state_size(self):
        return self._num_units
    
    def __call__(self, inputs, state, scope=None):
        """Summation for dynamic programming. Inputs are the
        log-potentials. States are the results of the summation at the
        last step"""
        with tf.variable_scope(scope or type(self).__name__):
            # add states and log-potentials
            multiples = [1] * (len(state.get_shape()) + 1)
            multiples[-1] = self.n_tags
            exp_state = tf.tile(tf.expand_dims(state, -1), multiples)
            added = exp_state + inputs
            # log-sum along first dimension (after the batch dim)
            max_val = tf.reduce_max(added)
            added_exp = tf.exp(added - max_val)
            summed_exp = tf.reduce_sum(added_exp, 1)
            new_state = tf.log(summed_exp) + max_val
        return new_state


# computing the log partition for a sequence of length config.num_steps
def log_partition(potentials, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    inputs_list = [tf.reshape(x, [batch_size] + pots_shape)
                   for x in tf.split(1, num_steps, potentials)]
    # forward pass
    sum_cell = CRFSumCell(config)
    state = tf.zeros([batch_size] + pots_shape[:-1])
    partial_sums = [0] * len(inputs_list)
    for t, input_ in enumerate(inputs_list):
        state = sum_cell(inputs_list[t], state)
        partial_sums[t] = state
    # sum at the end
    max_val = tf.reduce_max(state)
    state_exp = tf.exp(state - max_val)
    log_part = tf.log(tf.reduce_sum(tf.reshape(state_exp, [batch_size, -1]), 1)) + max_val
    return tf.reduce_sum(log_part)


# compute the log to get the log-likelihood
def log_score(potentials, window_indices, mask, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    flat_pots = tf.reshape(potentials, [-1])
    flat_scores = tf.gather(flat_pots, window_indices)
    scores = tf.reshape(flat_scores, [batch_size, num_steps])
    scores = tf.mul(scores, mask)
    return tf.reduce_sum(scores)
    

# TODO: alpha-beta rec
def marginals(potentials, config):
    batch_size = int(potentials.get_shape()[0])
    num_steps = int(potentials.get_shape()[1])
    pots_shape = map(int, potentials.get_shape()[2:])
    inputs_list = [tf.reshape(x, [batch_size] + pots_shape)
                   for x in tf.split(1, num_steps, potentials)]
    # forward and backwar pass
    sum_cell_f = CRFSumCell(config)
    sum_cell_b = CRFSumCell(config)
    state_f = tf.convert_to_tensor(np.zeros(pots_shape[:-1]))
    state_b = tf.convert_to_tensor(np.zeros(pots_shape[:-1]))
    partial_sums_f = [0] * len(inputs_list)
    partial_sums_b = [0] * len(inputs_list)
    for t, _ in enumerate(inputs_list):
        state_f = sum_cell_f(inputs_list[t], state_f)
        partial_sums_f[t] = state_f
        state_b = sum_cell_b(inputs_list[t], state_b)
        partial_sums_b[-1 - t] = state_b
    # TODO: compute marginals
    marginals = 0
    return marginals


###################################
# Making a (deep) CRF             #
###################################
class CRF:
    def __init__(self, config):
        self.batch_size = config.batch_size
        self.num_steps = config.num_steps
        num_features = len(config.input_features)
        # input_ids <- batch.features
        self.input_ids = tf.placeholder(tf.int32, shape=[self.batch_size,
                                                         self.num_steps,
                                                         num_features])
        # mask <- batch.mask
        self.mask = tf.placeholder(tf.float32, [self.batch_size, self.num_steps])
        # pot_indices <- batch.tag_neighbours_lin
        self.pot_indices = tf.placeholder(tf.int32,
                                          [config.batch_size * config.num_steps])
        # targets <- batch.tags_one_hot
        self.targets = tf.placeholder(tf.float32, [config.batch_size,
                                                   config.num_steps,
                                                   config.n_tags])
        # window_indices <- batch.tag_windows_lin
        self.window_indices = tf.placeholder(tf.int32,
                                             [config.batch_size * config.num_steps])

    def make(self, config, params, reuse=False, name='CRF'):
        # TODO: add marginal inference
        with tf.variable_scope(name):
            if reuse:
                tf.get_variable_scope().reuse_variables()
            # out_layer <- output of NN (TODO: add layers)
            (out_layer, embeddings) = feature_layer(self.input_ids,
                                                    config, params,
                                                    reuse=reuse)
            params.embeddings = embeddings
            if config.verbose:
                print('features layer done')
            self.out_layer = out_layer
            # pots_layer <- potentials
            (pots_layer, W_pot, b_pot) = potentials_layer(out_layer,
                                                          self.mask,
                                                          config, params,
                                                          reuse=reuse)
            params.W_pot = W_pot
            params.b_pot = b_pot
            if config.verbose:
                print('potentials layer done')
            self.pots_layer = pots_layer
            # pseudo-log-likelihood
            conditional, pseudo_ll = pseudo_likelihood(pots_layer,
                                                       self.pot_indices,
                                                       self.targets, config)
            self.pseudo_ll = pseudo_ll
            # accuracy of p(t_i | t_{i-1}, t_{i+1})
            correct_cond_pred = tf.equal(tf.argmax(conditional, 2), tf.argmax(self.targets, 2))
            correct_cond_pred = tf.cast(correct_cond_pred,"float")
            cond_accuracy = tf.reduce_sum(correct_cond_pred * tf.reduce_sum(self.targets, 2)) /\
                            tf.reduce_sum(self.targets)
            self.cond_accuracy = cond_accuracy
            # log-likelihood
            log_sc = log_score(self.pots_layer, self.window_indices,
                               self.mask, config)
            log_part = log_partition(self.pots_layer, config)
            log_likelihood = log_sc - log_part
            self.log_likelihood = log_likelihood
            # L1 regularization
            self.l1_norm = tf.reduce_sum(tf.zeros([1]))
            for feat in config.l1_list:
                self.l1_norm += config.l1_reg * \
                                tf.reduce_sum(tf.abs(params.embeddings[feat]))
            # L2 regularization
            self.l2_norm = tf.reduce_sum(tf.zeros([1]))
            for feat in config.l2_list:
                self.l2_norm += config.l2_reg * \
                                tf.reduce_sum(tf.mul(params.embeddings[feat],
                                                     params.embeddings[feat]))
            # map assignment and accuracy of map assignment
            map_tags = map_assignment(self.pots_layer, config)
            correct_pred = tf.equal(map_tags, tf.argmax(self.targets, 2))
            correct_pred = tf.cast(correct_pred,"float")
            accuracy = tf.reduce_sum(correct_pred * tf.reduce_sum(self.targets, 2)) /\
                       tf.reduce_sum(self.targets)
            self.map_tags = map_tags
            self.accuracy = accuracy
    
    def train_epoch(self, data, config, params, session, crit_type='likelihood'):
        batch_size = config.batch_size
        criterion = None
        if crit_type == 'pseudo':
            criterion = -self.pseudo_ll
        else:
            criterion = -self.log_likelihood
        criterion -= config.l1_reg * self.l1_norm + config.l1_reg * self.l2_norm
        train_step = tf.train.AdagradOptimizer(config.learning_rate).minimize(criterion)
        session.run(tf.initialize_all_variables())
        # TODO: gradient clipping
        total_crit = 0.
        n_batches = len(data) / batch_size
        batch = Batch()
        for i in range(n_batches):
            batch.read(data, i * batch_size, config)
            f_dict = {self.input_ids: batch.features,
                      self.pot_indices: batch.tag_neighbours_lin,
                      self.window_indices: batch.tag_windows_lin,
                      self.mask: batch.mask,
                      self.targets: batch.tags_one_hot}
            train_step.run(feed_dict=f_dict)
            crit = criterion.eval(feed_dict=f_dict)
            total_crit += crit
            if i % 50 == 0:
                train_accuracy = self.accuracy.eval(feed_dict=f_dict)
                print i, n_batches, train_accuracy, crit
                print("step %d of %d, training accuracy %f, criterion %f" %
                      (i, n_batches, train_accuracy, crit))
        print 'total crit', total_crit / n_batches
        return total_crit / n_batches
    
    def validate_accuracy(self, data, config):
        batch_size = config.batch_size
        batch = Batch()
        total_accuracy = 0.
        total_cond_accuracy = 0.
        total = 0.
        for i in range(len(data) / batch_size):
            batch.read(data, i * batch_size, config)
            f_dict = {self.input_ids: batch.features,
                      self.targets: batch.tags_one_hot,
                      self.pot_indices: batch.tag_neighbours_lin}
            dev_accuracy = self.accuracy.eval(feed_dict=f_dict)
            dev_cond_accuracy = self.cond_accuracy.eval(feed_dict=f_dict)
            pll = self.pseudo_ll.eval(feed_dict=f_dict)
            ll = self.log_likelihood.eval(feed_dict=f_dict)
            total_accuracy += dev_accuracy
            total_cond_accuracy += dev_cond_accuracy
            total_pll += pll
            total_ll += ll
            total += 1
            if i % 100 == 0:
                print("%d of %d: \t map accuracy: %f \t cond accuracy: %f \
                       \t pseudo_ll:  %f \t log_likelihood:  %f" % (i, len(data) / batch_size,
                                                total_accuracy / total,
                                                total_cond_accuracy / total))
        return (total_accuracy / total, total_cond_accuracy / total)

