import pprint
from gurobipy import *
import numpy as np
import json
import time
from tqdm import tqdm


"""
This code is for the most part written by Hongge Chen and is taken and
adapted from the repository https://github.com/chenhongge/RobustTrees.

It is an implementation of the MILP attack from:
Kantchelian, Alex, J. Doug Tygar, and Anthony Joseph. "Evasion and hardening of
tree ensemble classifiers." International Conference on Machine Learning.
PMLR, 2016.

Feasibility idea from:
Andriushchenko, Maksym, and Matthias Hein. "Provably robust boosted decision 
stumps and trees against adversarial attacks." arXiv preprint arXiv:1906.03526
(2019).

The changes made were related to:
- Default guard_val, round_digits values
- Removing dependency on xgboost
- Taking only a JSON file as input
- Solving a feasibility encoding for fixed epsilon
- Removing print statements
- Removing excessive model updates
- Only keeping binary classification attacks
"""


GUARD_VAL = 5e-6
ROUND_DIGITS = 6


class node_wrapper(object):
    def __init__(
        self,
        treeid,
        nodeid,
        attribute,
        threshold,
        left_leaves,
        right_leaves,
        root=False,
    ):
        # left_leaves and right_leaves are the lists of leaf indices in self.leaf_v_list
        self.attribute = attribute
        self.threshold = threshold
        self.node_pos = []
        self.leaves_lists = []
        self.add_leaves(treeid, nodeid, left_leaves, right_leaves, root)

    def print(self):
        print(
            "node_pos{}, attr:{}, th:{}, leaves:{}".format(
                self.node_pos, self.attribute, self.threshold, self.leaves_lists
            )
        )

    def add_leaves(self, treeid, nodeid, left_leaves, right_leaves, root=False):
        self.node_pos.append({"treeid": treeid, "nodeid": nodeid})
        if root:
            self.leaves_lists.append((left_leaves, right_leaves, "root"))
        else:
            self.leaves_lists.append((left_leaves, right_leaves))

    def add_grb_var(self, node_grb_var, leaf_grb_var_list):
        self.p_grb_var = node_grb_var
        self.l_grb_var_list = []
        for item in self.leaves_lists:
            left_leaf_grb_var = [leaf_grb_var_list[i] for i in item[0]]
            right_leaf_grb_var = [leaf_grb_var_list[i] for i in item[1]]
            if len(item) == 3:
                self.l_grb_var_list.append(
                    (left_leaf_grb_var, right_leaf_grb_var, "root")
                )
            else:
                self.l_grb_var_list.append((left_leaf_grb_var, right_leaf_grb_var))


class KantchelianAttack(object):
    def __init__(
        self,
        json_model,
        epsilon=None,
        order=np.inf,
        guard_val=GUARD_VAL,
        round_digits=ROUND_DIGITS,
        pos_json_input=None,
        neg_json_input=None,
        pred_threshold=0.0,
        verbose=False,
        n_threads=1,
    ):
        assert epsilon is None or order == np.inf, "feasibility epsilon can only be used with order inf"

        self.pred_threshold = pred_threshold
        self.epsilon = epsilon
        self.binary = (pos_json_input == None) or (neg_json_input == None)
        self.pos_json_input = pos_json_input
        self.neg_json_input = neg_json_input
        self.guard_val = guard_val
        self.round_digits = round_digits
        self.json_model = json_model
        self.order = order
        self.verbose = verbose
        self.n_threads = n_threads

        # two nodes with identical decision are merged in this list, their left and right leaves and in the list, third element of the tuple
        self.node_list = []
        self.leaf_v_list = []  # list of all leaf values
        self.leaf_pos_list = []  # list of leaves' position in xgboost model
        self.leaf_count = [0]  # total number of leaves in the first i trees
        node_check = (
            {}
        )  # track identical decision nodes. {(attr, th):<index in node_list>}

        def dfs(tree, treeid, root=False, neg=False):
            if "leaf" in tree.keys():
                if neg:
                    self.leaf_v_list.append(-tree["leaf"])
                else:
                    self.leaf_v_list.append(tree["leaf"])
                self.leaf_pos_list.append({"treeid": treeid, "nodeid": tree["nodeid"]})
                return [len(self.leaf_v_list) - 1]
            else:
                attribute, threshold, nodeid = (
                    tree["split"],
                    tree["split_condition"],
                    tree["nodeid"],
                )
                if type(attribute) == str:
                    attribute = int(attribute[1:])
                threshold = round(threshold, self.round_digits)
                # XGBoost can only offer precision up to 8 digits, however, minimum difference between two splits can be smaller than 1e-8
                # here rounding may be an option, but its hard to choose guard value after rounding
                # for example, if round to 1e-6, then guard value should be 5e-7, or otherwise may cause mistake
                # xgboost prediction has a precision of 1e-8, so when min_diff<1e-8, there is a precision problem
                # if we do not round, xgboost.predict may give wrong results due to precision, but manual predict on json file should always work
                left_subtree = None
                right_subtree = None
                for subtree in tree["children"]:
                    if subtree["nodeid"] == tree["yes"]:
                        left_subtree = subtree
                    if subtree["nodeid"] == tree["no"]:
                        right_subtree = subtree
                if left_subtree == None or right_subtree == None:
                    pprint.pprint(tree)
                    raise ValueError("should be a tree but one child is missing")
                left_leaves = dfs(left_subtree, treeid, False, neg)
                right_leaves = dfs(right_subtree, treeid, False, neg)
                if (attribute, threshold) not in node_check:
                    self.node_list.append(
                        node_wrapper(
                            treeid,
                            nodeid,
                            attribute,
                            threshold,
                            left_leaves,
                            right_leaves,
                            root,
                        )
                    )
                    node_check[(attribute, threshold)] = len(self.node_list) - 1
                else:
                    node_index = node_check[(attribute, threshold)]
                    self.node_list[node_index].add_leaves(
                        treeid, nodeid, left_leaves, right_leaves, root
                    )
                return left_leaves + right_leaves

        if self.binary:
            for i, tree in enumerate(self.json_model):
                dfs(tree, i, root=True)
                self.leaf_count.append(len(self.leaf_v_list))
            if len(self.json_model) + 1 != len(self.leaf_count):
                print("self.leaf_count:", self.leaf_count)
                raise ValueError("leaf count error")
        else:
            for i, tree in enumerate(self.pos_json_input):
                dfs(tree, i, root=True)
                self.leaf_count.append(len(self.leaf_v_list))
            for i, tree in enumerate(self.neg_json_input):
                dfs(tree, i + len(self.pos_json_input), root=True, neg=True)
                self.leaf_count.append(len(self.leaf_v_list))
            if len(self.pos_json_input) + len(self.neg_json_input) + 1 != len(
                self.leaf_count
            ):
                print("self.leaf_count:", self.leaf_count)
                raise ValueError("leaf count error")

        self.m = Model("attack")

        if not self.verbose:
            self.m.setParam(
                "OutputFlag", 0
            )  # suppress Gurobi output, gives a small speed-up and prevents huge logs

        self.m.setParam("Threads", self.n_threads)

        # Most datasets require a very low tolerance
        self.m.setParam("IntFeasTol", 1e-9)
        self.m.setParam("FeasibilityTol", 1e-9)
        
        self.P = self.m.addVars(len(self.node_list), vtype=GRB.BINARY, name="p")
        self.L = self.m.addVars(len(self.leaf_v_list), lb=0, ub=1, name="l")
        if epsilon:
            self.B = self.m.addVar(
                name="b", lb=0.0, ub=self.epsilon - 0.0001
            )
        elif self.order == np.inf:
            self.B = self.m.addVar(name="b")
        self.llist = [self.L[key] for key in range(len(self.L))]
        self.plist = [self.P[key] for key in range(len(self.P))]

        # p dictionary by attributes, {attr1:[(threshold1, gurobiVar1),(threshold2, gurobiVar2),...],attr2:[...]}
        self.pdict = {}
        for i, node in enumerate(self.node_list):
            node.add_grb_var(self.plist[i], self.llist)
            if node.attribute not in self.pdict:
                self.pdict[node.attribute] = [(node.threshold, self.plist[i])]
            else:
                self.pdict[node.attribute].append((node.threshold, self.plist[i]))

        # sort each feature list
        # add p constraints
        for key in self.pdict.keys():
            min_diff = 1000
            if len(self.pdict[key]) > 1:
                self.pdict[key].sort(key=lambda tup: tup[0])
                for i in range(len(self.pdict[key]) - 1):
                    self.m.addConstr(
                        self.pdict[key][i][1] <= self.pdict[key][i + 1][1],
                        name="p_consis_attr{}_{}th".format(key, i),
                    )
                    min_diff = min(
                        min_diff, self.pdict[key][i + 1][0] - self.pdict[key][i][0]
                    )

                if min_diff < 2 * self.guard_val:
                    self.guard_val = min_diff / 3
                    print(
                        "guard value too large, change to min_diff/3:", self.guard_val
                    )

        # all leaves sum up to 1
        for i in range(len(self.leaf_count) - 1):
            leaf_vars = [
                self.llist[j] for j in range(self.leaf_count[i], self.leaf_count[i + 1])
            ]
            self.m.addConstr(
                LinExpr([1] * (self.leaf_count[i + 1] - self.leaf_count[i]), leaf_vars)
                == 1,
                name="leaf_sum_one_for_tree{}".format(i),
            )

        # node leaves constraints
        for j in range(len(self.node_list)):
            p = self.plist[j]
            for k in range(len(self.node_list[j].leaves_lists)):
                left_l = [self.llist[i] for i in self.node_list[j].leaves_lists[k][0]]
                right_l = [self.llist[i] for i in self.node_list[j].leaves_lists[k][1]]
                if len(self.node_list[j].leaves_lists[k]) == 3:
                    self.m.addConstr(
                        LinExpr([1] * len(left_l), left_l) - p == 0,
                        name="p{}_root_left_{}".format(j, k),
                    )
                    self.m.addConstr(
                        LinExpr([1] * len(right_l), right_l) + p == 1,
                        name="p_{}_root_right_{}".format(j, k),
                    )
                else:
                    self.m.addConstr(
                        LinExpr([1] * len(left_l), left_l) - p <= 0,
                        name="p{}_left_{}".format(j, k),
                    )
                    self.m.addConstr(
                        LinExpr([1] * len(right_l), right_l) + p <= 1,
                        name="p{}_right_{}".format(j, k),
                    )
        self.m.update()

    def attack(self, X, label):
        x = np.copy(X)

        # model mislabel
        try:
            c = self.m.getConstrByName("mislabel")
            self.m.remove(c)
        except Exception:
            pass
        if (not self.binary) or label == 1:
            self.m.addConstr(
                LinExpr(self.leaf_v_list, self.llist) <= self.pred_threshold - self.guard_val,
                name="mislabel",
            )
        else:
            self.m.addConstr(
                LinExpr(self.leaf_v_list, self.llist)
                >= self.pred_threshold + self.guard_val,
                name="mislabel",
            )

        # Generate constraints for self.B, the l-infinity distance.
        for key in self.pdict.keys():
            if len(self.pdict[key]) == 0:
                raise ValueError("self.pdict list empty")
            axis = [-np.inf] + [item[0] for item in self.pdict[key]] + [np.inf]
            w = [0] * (len(self.pdict[key]) + 1)
            for i in range(len(axis) - 1, 0, -1):
                if x[key] < axis[i] and x[key] >= axis[i - 1]:
                    w[i - 1] = 0
                elif x[key] < axis[i] and x[key] < axis[i - 1]:
                    w[i - 1] = np.abs(x[key] - axis[i - 1])
                elif x[key] >= axis[i] and x[key] >= axis[i - 1]:
                    w[i - 1] = np.abs(x[key] - axis[i] + self.guard_val)
                else:
                    print("x[key]:", x[key])
                    print("axis:", axis)
                    print("axis[i]:{}, axis[i-1]:{}".format(axis[i], axis[i - 1]))
                    raise ValueError("wrong axis ordering")
            for i in range(len(w) - 1):
                w[i] -= w[i + 1]
            else:
                try:
                    c = self.m.getConstrByName("linf_constr_attr{}".format(key))
                    self.m.remove(c)
                except Exception:
                    pass
                self.m.addConstr(
                    LinExpr(w[:-1], [item[1] for item in self.pdict[key]]) + w[-1]
                    <= self.B,
                    name="linf_constr_attr{}".format(key),
                )

        self.m.setObjective(0, GRB.MINIMIZE)

        self.m.update()
        self.m.optimize()

        return self.m.status == 3  # 3 -> infeasible -> no adv example -> True

    def optimal_adversarial_example(self, sample, label):
        if self.binary:
            pred = 1 if self.check(sample, self.json_model) >= self.pred_threshold else 0
        else:
            pred = 1 if self.check(sample, self.pos_json_input) >= self.check(sample, self.neg_json_input) else 0
        x = np.copy(sample)

        if pred != label:
            # Wrong prediction, no attack
            return x

        # model mislabel
        # this is for binary
        try:
            c = self.m.getConstrByName("mislabel")
            self.m.remove(c)
            self.m.update()
        except Exception:
            pass
        if (not self.binary) or label == 1:
            self.m.addConstr(
                LinExpr(self.leaf_v_list, self.llist) <= self.pred_threshold - self.guard_val,
                name="mislabel",
            )
        else:
            self.m.addConstr(
                LinExpr(self.leaf_v_list, self.llist)
                >= self.pred_threshold + self.guard_val,
                name="mislabel",
            )
        self.m.update()

        if self.order == np.inf:
            rho = 1
        else:
            rho = self.order

        if self.order != np.inf:
            self.obj_coeff_list = []
            self.obj_var_list = []
            self.obj_c = 0
        # model objective
        for key in self.pdict.keys():
            if len(self.pdict[key]) == 0:
                raise ValueError("self.pdict list empty")
            axis = [-np.inf] + [item[0] for item in self.pdict[key]] + [np.inf]
            w = [0] * (len(self.pdict[key]) + 1)
            for i in range(len(axis) - 1, 0, -1):
                if x[key] < axis[i] and x[key] >= axis[i - 1]:
                    w[i - 1] = 0
                elif x[key] < axis[i] and x[key] < axis[i - 1]:
                    w[i - 1] = np.abs(x[key] - axis[i - 1]) ** rho
                elif x[key] >= axis[i] and x[key] >= axis[i - 1]:
                    w[i - 1] = np.abs(x[key] - axis[i] + self.guard_val) ** rho
                else:
                    print("x[key]:", x[key])
                    print("axis:", axis)
                    print("axis[i]:{}, axis[i-1]:{}".format(axis[i], axis[i - 1]))
                    raise ValueError("wrong axis ordering")
            for i in range(len(w) - 1):
                w[i] -= w[i + 1]
            if self.order != np.inf:
                self.obj_c += w[-1]
                self.obj_coeff_list += w[:-1]
                self.obj_var_list += [item[1] for item in self.pdict[key]]
            else:
                try:
                    c = self.m.getConstrByName("linf_constr_attr{}".format(key))
                    self.m.remove(c)
                    self.m.update()
                except Exception:
                    pass
                self.m.addConstr(
                    LinExpr(w[:-1], [item[1] for item in self.pdict[key]]) + w[-1]
                    <= self.B,
                    name="linf_constr_attr{}".format(key),
                )
                self.m.update()

        if self.order != np.inf:
            self.m.setObjective(
                LinExpr(self.obj_coeff_list, self.obj_var_list) + self.obj_c,
                GRB.MINIMIZE,
            )
        else:
            self.m.setObjective(self.B, GRB.MINIMIZE)

        self.m.update()
        self.m.optimize()

        # If infeasible
        if self.m.status == 3:
            return None

        # Assert that the adversarial example causes a misclassification
        for key in self.pdict.keys():
            for node in self.pdict[key]:
                if node[1].x > 0.5 and x[key] >= node[0]:
                    x[key] = node[0] - self.guard_val
                if node[1].x <= 0.5 and x[key] < node[0]:
                    x[key] = node[0] + self.guard_val

        if self.binary:
            pred = 1 if self.check(x, self.json_model) >= self.pred_threshold else 0
        else:
            pos_value = self.check(x, self.pos_json_input)
            neg_value = self.check(x, self.neg_json_input)
            pred = 1 if pos_value >= neg_value else 0

        if pred == label and self.verbose:
            print("!" * 50)
            print("MILP result did not cause a misclassification!")
            print("!" * 50)

        return x

    def check(self, x, json_file):
        # Due to XGBoost precision issues, some attacks may not succeed if tested using model.predict.
        # We manually run the tree on the json file here to make sure those attacks are actually successful.
        leaf_values = []
        for item in json_file:
            tree = item.copy()
            while "leaf" not in tree.keys():
                attribute, threshold, nodeid = (
                    tree["split"],
                    tree["split_condition"],
                    tree["nodeid"],
                )
                if type(attribute) == str:
                    attribute = int(attribute[1:])
                if x[attribute] < threshold:
                    if tree["children"][0]["nodeid"] == tree["yes"]:
                        tree = tree["children"][0].copy()
                    elif tree["children"][1]["nodeid"] == tree["yes"]:
                        tree = tree["children"][1].copy()
                    else:
                        pprint.pprint(tree)
                        print("x[attribute]:", x[attribute])
                        raise ValueError("child not found")
                else:
                    if tree["children"][0]["nodeid"] == tree["no"]:
                        tree = tree["children"][0].copy()
                    elif tree["children"][1]["nodeid"] == tree["no"]:
                        tree = tree["children"][1].copy()
                    else:
                        pprint.pprint(tree)
                        print("x[attribute]:", x[attribute])
                        raise ValueError("child not found")
            leaf_values.append(tree["leaf"])
        manual_res = np.sum(leaf_values)
        return manual_res


class KantchelianAttackMultiClass(object):
    def __init__(
        self,
        json_model,
        n_classes,
        order=np.inf,
        guard_val=GUARD_VAL,
        round_digits=ROUND_DIGITS,
        pred_threshold=0.0,
        verbose=False,
        n_threads=1
    ):
        if n_classes <= 2:
            raise ValueError('multiclass attack must be used when number of class > 2')

        self.n_classes = n_classes
        self.order = order

        # Create all attacker models, this takes quadratic space in terms
        # of n_classes, but speeds up attacks for many samples.
        one_vs_all_models = [[] for _ in range(self.n_classes)]
        for i, json_tree in enumerate(json_model):
            one_vs_all_models[i % n_classes].append(json_tree)

        self.binary_attackers = []
        for class_label in range(self.n_classes):
            attackers = []
            for other_label in range(self.n_classes):
                if class_label == other_label:
                    attackers.append(None)

                attacker = KantchelianAttack(
                    None,
                    epsilon=None,
                    order=order,
                    guard_val=guard_val,
                    round_digits=round_digits,
                    pred_threshold=pred_threshold,
                    verbose=verbose,
                    n_threads=n_threads,
                    pos_json_input=one_vs_all_models[class_label],
                    neg_json_input=one_vs_all_models[other_label],
                )

                attackers.append(attacker)
            self.binary_attackers.append(attackers)

    def optimal_adversarial_example(self, sample, label):
        best_distance = float("inf")
        best_adv_example = None

        for other_label in range(self.n_classes):
            if other_label == label:
                continue

            attacker = self.binary_attackers[label][other_label]
            adv_example = attacker.optimal_adversarial_example(sample, 1)

            if adv_example is not None:
                distance = np.linalg.norm(sample - adv_example, ord=self.order)
                if distance < best_distance:
                    best_adv_example = adv_example
                    best_distance = distance

        if best_adv_example is None:
            raise Exception("No adversarial example found, does your model predict a constant value?")
        
        return best_adv_example


def score_dataset(
    json_filename,
    X,
    y,
    guard_val=GUARD_VAL,
    round_digits=ROUND_DIGITS,
    sample_limit=500,
    pred_threshold=0.0,
):
    """
    Scores the tree ensemble in JSON format on the given dataset (samples X, labels y)
    using the regular accuracy score.
    
    Parameters
    ----------
    json_filename : str
        Path to the JSON file export of a decision tree ensemble.
    X : array-like of shape (n_samples, n_features)
        The training samples.
    y : array-like of shape (n_samples,)
        The class labels as integers 0 (benign) or 1 (malicious).
    guard_val : float, optional (default=GUARD_VAL)
        Guard value to combat inaccuracy between JSON and GUROBI floats.
        For example if the prediction threshold is 0.5, the solver needs to reach
        a threshold of 0.5 + guard_val or 0.5 - guard_val for it to count.
    round_digits : int, optional (default=ROUND_DIGITS)
        Number of digits to round threshold values to in order to combat the inaccuracy
        between JSON and GUROBI floats.
    sample_limit : int, optional (default=500)
        Maximum number of samples to attack, useful for large datasets.
    pred_threshold : float, optional (default=0.5)
        Threshold for predicting class labels 0/1. For random forests and
        decision trees this value should be 0.5. For tree ensembles such as
        gradient boosting that often use a sigmoid function, this value
        should be 0.0.

    Returns
    -------
    accuracy : float
        Regular accuracy score for the model on this dataset.
    """
    json_model = json.load(open(json_filename, "r"))

    attack = KantchelianAttack(
        json_model,
        guard_val=guard_val,
        round_digits=round_digits,
        pred_threshold=pred_threshold,
    )

    X = X[:sample_limit]
    y = y[:sample_limit]

    n_correct = 0
    for sample, label in zip(X, y):
        predict = 1 if attack.check(sample, json_model) >= pred_threshold else 0
        if label != predict:
            continue
        else:
            n_correct += 1

    return n_correct / len(X)
    

def attack_json_for_X_y(
    json_filename,
    X,
    y,
    order=np.inf,
    guard_val=GUARD_VAL,
    round_digits=ROUND_DIGITS,
    sample_limit=None,
    pred_threshold=0.5,
    n_threads=8,
    verbose=True,
):
    """
    Find minimal adversarial examples on the given tree ensemble in JSON
    format using Kantchelian's MILP attack.

    Parameters
    ----------
    json_filename : str
        Path to the JSON file export of a decision tree ensemble.
    X : array-like of shape (n_samples, n_features)
        The adversarial victims.
    y : array-like of shape (n_samples,)
        The class labels as integers 0 or 1.
    order : {0, 1, 2, np.inf}, optional (default=np.inf)
        Order of the L norm.
    guard_val : float, optional (default=GUARD_VAL)
        Guard value to combat inaccuracy between JSON and GUROBI floats.
        For example if the prediction threshold is 0.5, the solver needs to reach
        a threshold of 0.5 + guard_val or 0.5 - guard_val for it to count.
    round_digits : int, optional (default=ROUND_DIGITS)
        Number of digits to round threshold values to in order to combat the inaccuracy
        between JSON and GUROBI floats.
    sample_limit : int, optional (default=None)
        Maximum number of samples to attack, to limit execution time on large datasets.
        If None, all samples from X, y are used.
    pred_threshold : float, optional (default=0.5)
        Threshold for predicting class labels 0/1. For random forests and
        decision trees this value should be 0.5. For tree ensembles such as
        gradient boosting that often use a sigmoid function, this value
        should be 0.0.
    n_threads : int, optional (default=8)
        Number of threads to use in the solver. For large / deep ensembles
        a value higher than 1 can speed up the search significantly.
    verbose : bool, optional (default=True)
        Whether the solver outputs solving progress.

    Returns
    -------
    avg_distance : float
        Average distance of the optimal adversarial examples calculated with norm "order"
    avg_time: float
        Average time it took to attack a victim
    adv_examples : array-like of shape (n_samples, n_features)
        Minimal adversarial examples (only the features).
    """

    json_model = json.load(open(json_filename, "r"))

    X = X[:sample_limit]
    y = y[:sample_limit]

    attack = KantchelianAttack(
        json_model,
        guard_val=guard_val,
        round_digits=round_digits,
        pred_threshold=pred_threshold,
        order=order,
        verbose=verbose,
        n_threads=n_threads,
    )

    t_0 = time.time()

    avg_dist = 0
    optimal_examples = []
    for sample, label in zip(X, y):
        optimal_ae_features = attack.optimal_adversarial_example(sample, label)
        distance = np.linalg.norm(sample - optimal_ae_features, order)

        avg_dist += distance
        optimal_examples.append(optimal_ae_features)

    t_1 = time.time()
    return avg_dist / len(optimal_examples), (t_1 - t_0) / len(optimal_examples), np.array(optimal_examples)


def optimal_adversarial_example(
    json_filename,
    sample,
    label,
    order=np.inf,
    n_classes=2,
    guard_val=GUARD_VAL,
    round_digits=ROUND_DIGITS,
    pred_threshold=0.0,
    n_threads=8,
    verbose=True,
):
    """
    Find a minimal adversarial example on the given tree ensemble in JSON
    format using Kantchelian's MILP attack.
    
    Parameters
    ----------
    json_filename : str
        Path to the JSON file export of a decision tree ensemble.
    sample : array-like of shape (n_features)
        Original sample.
    label : int
        Original label (0 or 1).
    order : {0, 1, 2, np.inf}, optional (default=np.inf)
        Order of the L norm.
    n_classes : int, optional
        Number of classes that the model can predict.
    guard_val : float, optional (default=GUARD_VAL)
        Guard value to combat inaccuracy between JSON and GUROBI floats.
        For example if the prediction threshold is 0.5, the solver needs to reach
        a threshold of 0.5 + guard_val or 0.5 - guard_val for it to count.
    round_digits : int, optional (default=ROUND_DIGITS)
        Number of digits to round threshold values to in order to combat the inaccuracy
        between JSON and GUROBI floats.
    pred_threshold : float, optional (default=0.5)
        Threshold for predicting class labels 0/1. For random forests and
        decision trees this value should be 0.5. For tree ensembles such as
        gradient boosting that often use a sigmoid function, this value
        should be 0.0.
    n_threads : int, optional (default=8)
        Number of threads to use in the solver. For large / deep ensembles
        a value higher than 1 can speed up the search significantly.
    verbose : bool, optional (default=True)
        Whether the solver outputs solving progress.

    Returns
    -------
    adv_example : array-like of shape (n_features)
        Minimal adversarial example.
    """
    json_model = json.load(open(json_filename, "r"))

    if n_classes == 2:
        attack = KantchelianAttack(
            json_model,
            guard_val=guard_val,
            round_digits=round_digits,
            pred_threshold=pred_threshold,
            order=order,
            verbose=verbose,
            n_threads=n_threads,
        )
    else:
        attack = KantchelianAttackMultiClass(
            json_model,
            n_classes,
            guard_val=guard_val,
            round_digits=round_digits,
            pred_threshold=pred_threshold,
            order=order,
            verbose=verbose,
            n_threads=n_threads,
        )

    return attack.optimal_adversarial_example(sample, label)


def attack_epsilon_feasibility(
    json_filename,
    X,
    y,
    epsilon,
    guard_val=GUARD_VAL,
    round_digits=ROUND_DIGITS,
    sample_limit=None,
    pred_threshold=0.0,
):
    """
    Scores the tree ensemble in JSON format on the given dataset (samples X, labels y)
    using the adversarial accuracy score. It uses Kantchelian's MILP attack but
    in its feasibility variant to check if attacks fail or succeed.
    
    Parameters
    ----------
    json_filename : str
        Path to the JSON file export of a decision tree ensemble.
    X : array-like of shape (n_samples, n_features)
        The training samples.
    y : array-like of shape (n_samples,)
        The class labels as integers 0 (benign) or 1 (malicious).
    epsilon : float
        L-infinity radius in which samples can be perturbed.
    guard_val : float, optional (default=GUARD_VAL)
        Guard value to combat inaccuracy between JSON and GUROBI floats.
        For example if the prediction threshold is 0.5, the solver needs to reach
        a threshold of 0.5 + guard_val or 0.5 - guard_val for it to count.
    round_digits : int, optional (default=ROUND_DIGITS)
        Number of digits to round threshold values to in order to combat the inaccuracy
        between JSON and GUROBI floats.
    sample_limit : int, optional (default=None)
        Maximum number of samples to attack, to limit execution time on large datasets.
        If None, all samples from X, y are used.
    pred_threshold : float, optional (default=0.5)
        Threshold for predicting class labels 0/1. For random forests and
        decision trees this value should be 0.5. For tree ensembles such as
        gradient boosting that often use a sigmoid function, this value
        should be 0.0.

    Returns
    -------
    adv_accuracy : float
        Adversarial accuracy score for the model on this dataset.
    """
    json_model = json.load(open(json_filename, "r"))

    attack = KantchelianAttack(
        json_model,
        epsilon,
        guard_val=guard_val,
        round_digits=round_digits,
        pred_threshold=pred_threshold,
    )

    if sample_limit:
        X = X[:sample_limit]
        y = y[:sample_limit]

    n_correct_within_epsilon = 0
    global_start = time.time()
    progress_bar = tqdm(total=X.shape[0])
    for sample, label in zip(X, y):
        correct_within_epsilon = attack.attack(sample, label)
        if correct_within_epsilon:
            n_correct_within_epsilon += 1

        progress_bar.update()
    progress_bar.close()

    total_time = time.time() - global_start
    print("Total time:", total_time)
    print("Avg time per instance:", total_time / len(X))

    adv_accuracy = n_correct_within_epsilon / len(X)

    return adv_accuracy
